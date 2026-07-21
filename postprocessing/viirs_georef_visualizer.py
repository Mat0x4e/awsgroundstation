#!/usr/bin/env python3
"""
viirs_georef_visualizer.py
==========================
Visualisation géoréférencée de granules VIIRS Level 1 (sortie IPOPP).

Produit :
  - PNG haute résolution avec côtes, frontières, grille lat/lon, annotations
  - GeoTIFF optionnel (nécessite rasterio)
  - JSON de métadonnées

Usage :
  python viirs_georef_visualizer.py \
      --sdr  SVI01_npp_d20240610_t1400000_e1401242_b00001_c00001_all.h5 \
             SVI02_npp_... SVI03_npp_... \
      --geo  GIGTO_npp_d20240610_t1400000_e1401242_b00001_c00001_all.h5 \
      --mode truecolor \
      --out  output/viirs_caribbean_truecolor.png \
      --destripe

  # Canal thermique (bande M15) :
  python viirs_georef_visualizer.py \
      --sdr  SVOM15_npp_... \
      --geo  GMODO_npp_... \
      --mode thermal \
      --out  output/viirs_caribbean_thermal.png

Auteur : généré par Claude (Anthropic) — adapté depuis aws-samples/aws-groundstation-s3-data-delivery
"""

import argparse
import json
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np
import numpy.ma as ma
import matplotlib
matplotlib.use("Agg")  # backend non-interactif pour serveur sans display
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.ticker as mticker

try:
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from cartopy.mpl.gridliner import LONGITUDE_FORMATTER, LATITUDE_FORMATTER
    HAS_CARTOPY = True
except ImportError:
    HAS_CARTOPY = False
    print("[WARN] cartopy non disponible — sortie sans projection cartographique")

try:
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.crs import CRS as RioCRS
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False

warnings.filterwarnings("ignore", category=UserWarning)

# ── Constantes radiométriques ──────────────────────────────────────────────────

# Constantes de Planck pour conversion radiance → température de brillance
PLANCK_C1 = 1.191042e8   # mW µm⁴ m⁻² sr⁻¹
PLANCK_C2 = 1.4387752e4  # µm K

# Longueur d'onde centrale bande M15 VIIRS (µm)
VIIRS_M15_CWL = 10.7630

# Fill value VIIRS Level 1
VIIRS_FILL = -999.3
VIIRS_FILL_UINT = 65535  # fill value entier 16 bits

# Îles et points d'intérêt à annoter dans la zone Caraïbes / Pacifique
POI_LABELS = {
    "Jamaïque":            (17.9,  -77.3),
    "Cuba":                (21.5,  -79.5),
    "Haïti":               (18.9,  -72.7),
    "Rép. Dominicaine":    (18.7,  -70.2),
    "Porto Rico":          (18.2,  -66.5),
    "Yucatán":             (20.5,  -89.0),
    "Floride":             (27.5,  -81.5),
    "Guatemala":           (15.5,  -90.3),
    "Belize":              (17.2,  -88.5),
    "Hawaï":               (20.5, -157.0),
    "îles Canaries":       (28.1,  -15.4),
    # Pacifique central
    "Californie":          (37.0, -120.0),
    "Mexique (Pacifique)": (20.0, -105.0),
}

# ── Fonctions utilitaires ──────────────────────────────────────────────────────

def read_viirs_metadata(h5file):
    """Lit les attributs globaux de la granule VIIRS."""
    meta = {}
    with h5py.File(h5file, "r") as f:
        attrs = f.attrs
        meta["granule_id"]    = attrs.get("N_Granule_ID", [b"unknown"])[0]
        meta["orbit_number"]  = attrs.get("N_Beginning_Orbit_Number", [0])[0]
        iet = attrs.get("N_Beginning_Time_IET", [0])[0]
        # IET = microsecondes depuis 1958-01-01 00:00:00 UTC
        # Offset IET → Unix epoch (secondes)
        IET_EPOCH_OFFSET = 378691200  # secondes entre 1958-01-01 et 1970-01-01
        ts_unix = iet / 1e6 - IET_EPOCH_OFFSET
        meta["datetime_utc"] = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
        meta["datetime_str"] = meta["datetime_utc"].strftime("%Y-%m-%d %H:%M:%S UTC")
        if isinstance(meta["granule_id"], bytes):
            meta["granule_id"] = meta["granule_id"].decode()
    return meta


def read_geo(geo_file, band_type="I"):
    """
    Lit les tableaux lat/lon depuis un fichier de géolocalisation VIIRS.

    band_type : 'I' → VIIRS-IMG-GEO_All  (bandes I · 375m)
                'M' → VIIRS-MOD-GEO_All  (bandes M · 750m)
    """
    group = f"VIIRS-{'IMG' if band_type == 'I' else 'MOD'}-GEO_All"
    with h5py.File(geo_file, "r") as f:
        lat_raw = f[f"/All_Data/{group}/Latitude"][:]
        lon_raw = f[f"/All_Data/{group}/Longitude"][:]

    # Masquer les fill values
    lat = ma.masked_where(lat_raw < -900, lat_raw.astype(np.float32))
    lon = ma.masked_where(lon_raw < -900, lon_raw.astype(np.float32))

    print(f"[GEO]  Shape lat/lon : {lat.shape}")
    print(f"[GEO]  Lat  : {float(lat.min()):.2f}° → {float(lat.max()):.2f}°")
    print(f"[GEO]  Lon  : {float(lon.min()):.2f}° → {float(lon.max()):.2f}°")

    return lat, lon


def read_reflectance_band(sdr_file, band_key):
    """
    Lit une bande de réflectance VIIRS et applique scale/offset.
    Retourne un tableau float32 en [0, 1] (masqué sur fill values).
    """
    with h5py.File(sdr_file, "r") as f:
        group_keys = [k for k in f["/All_Data"].keys() if "SDR" in k]
        if not group_keys:
            raise ValueError(f"Aucun groupe SDR trouvé dans {sdr_file}")
        group = group_keys[0]

        raw  = f[f"/All_Data/{group}/Reflectance"][:]
        fac  = f[f"/All_Data/{group}/Reflectance_Factors"][:]
        scale, offset = float(fac[0]), float(fac[1])

    # Masquer fill values entiers
    raw_ma = ma.masked_where(raw == VIIRS_FILL_UINT, raw.astype(np.float32))
    refl   = raw_ma * scale + offset
    refl   = ma.clip(refl, 0.0, 1.0)

    print(f"[SDR]  {band_key} shape={raw.shape}  "
          f"réfl min={float(refl.min()):.4f} max={float(refl.max()):.4f}")
    return refl


def read_radiance_band(sdr_file):
    """
    Lit une bande de radiance VIIRS (canal thermique M15) et convertit
    en température de brillance (Kelvin).
    """
    with h5py.File(sdr_file, "r") as f:
        group_keys = [k for k in f["/All_Data"].keys() if "SDR" in k]
        group = group_keys[0]

        raw  = f[f"/All_Data/{group}/Radiance"][:]
        fac  = f[f"/All_Data/{group}/Radiance_Factors"][:]
        scale, offset = float(fac[0]), float(fac[1])

    raw_ma = ma.masked_where(raw == VIIRS_FILL_UINT, raw.astype(np.float32))
    rad    = raw_ma * scale + offset  # mW m⁻² sr⁻¹ µm⁻¹

    # Conversion radiance → température de brillance (loi de Planck inversée)
    # BT = C2 / (λ × ln(C1 / (λ⁵ × L) + 1))
    lam = VIIRS_M15_CWL
    with np.errstate(divide="ignore", invalid="ignore"):
        bt = PLANCK_C2 / (lam * np.log(PLANCK_C1 / (lam**5 * rad) + 1.0))

    bt = ma.masked_where(rad.mask if ma.is_masked(rad) else False, bt)
    print(f"[SDR]  M15 BT : {float(bt.min()):.1f}K → {float(bt.max()):.1f}K")
    return bt


def gamma_stretch(band, gamma=0.5, p_low=2, p_high=98):
    """
    Correction gamma + étirement de contraste par percentiles.
    Retourne un tableau float32 en [0, 1].
    """
    valid = band.compressed() if ma.is_masked(band) else band.ravel()
    vmin  = np.percentile(valid, p_low)
    vmax  = np.percentile(valid, p_high)
    stretched = ma.clip((band - vmin) / (vmax - vmin + 1e-10), 0.0, 1.0)
    corrected = stretched ** gamma
    return corrected


def destripe(data):
    """
    Déstripage par soustraction de la médiane par détecteur.
    VIIRS a 16 détecteurs par bande → blocs de 16 lignes.
    """
    result = data.copy().astype(np.float32)
    n_det  = 16
    nrows  = data.shape[0]
    for det in range(n_det):
        rows = range(det, nrows, n_det)
        col_medians = ma.median(data[rows, :], axis=0)
        global_med  = float(ma.median(col_medians))
        for r in rows:
            result[r, :] -= (col_medians - global_med)
    print("[DESTRIPE]  Correction inter-détecteurs appliquée")
    return ma.array(result, mask=ma.getmaskarray(data))


def build_rgb(r_band, g_band, b_band, gamma=0.5):
    """Construit un tableau RGB (H, W, 3) normalisé."""
    r = gamma_stretch(r_band, gamma=gamma)
    g = gamma_stretch(g_band, gamma=gamma)
    b = gamma_stretch(b_band, gamma=gamma)

    # Assembler et remplir les pixels masqués avec NaN pour transparence
    H, W = r.shape
    rgb  = np.full((H, W, 3), np.nan, dtype=np.float32)
    mask_combined = (ma.getmaskarray(r) |
                     ma.getmaskarray(g) |
                     ma.getmaskarray(b))
    rgb[~mask_combined, 0] = r.data[~mask_combined]
    rgb[~mask_combined, 1] = g.data[~mask_combined]
    rgb[~mask_combined, 2] = b.data[~mask_combined]
    return rgb


def annotate_pois(ax, lat, lon, projection):
    """
    Annote les points d'intérêt géographiques visibles dans le swath.
    """
    lat_min, lat_max = float(lat.min()), float(lat.max())
    lon_min, lon_max = float(lon.min()), float(lon.max())
    margin = 2.0

    for name, (poi_lat, poi_lon) in POI_LABELS.items():
        if (lat_min - margin <= poi_lat <= lat_max + margin and
                lon_min - margin <= poi_lon <= lon_max + margin):
            ax.text(
                poi_lon, poi_lat, name,
                transform=ccrs.PlateCarree(),
                fontsize=7, color="white",
                ha="center", va="center",
                bbox=dict(boxstyle="round,pad=0.2",
                          facecolor="black", alpha=0.45,
                          edgecolor="none"),
                zorder=10
            )


def save_geotiff(data, lat, lon, out_path, mode):
    """
    Exporte le raster en GeoTIFF avec CRS WGS84.
    (Reprojection simplifiée sur grille régulière par interpolation.)
    """
    if not HAS_RASTERIO:
        print("[WARN] rasterio non disponible — GeoTIFF non produit")
        return

    from scipy.interpolate import griddata

    lat_min, lat_max = float(lat.min()), float(lat.max())
    lon_min, lon_max = float(lon.min()), float(lon.max())

    # Grille de sortie régulière ~750m (~0.0067°)
    res = 0.0067
    lon_grid = np.arange(lon_min, lon_max, res)
    lat_grid = np.arange(lat_max, lat_min, -res)
    lon_2d, lat_2d = np.meshgrid(lon_grid, lat_grid)

    points = np.column_stack([lon.compressed(), lat.compressed()])
    out_path_tiff = str(out_path).replace(".png", ".tif")

    if mode == "thermal":
        values = data.compressed()
        grid = griddata(points, values, (lon_2d, lat_2d), method="linear")
        transform = from_bounds(lon_min, lat_min, lon_max, lat_max,
                                grid.shape[1], grid.shape[0])
        with rasterio.open(
            out_path_tiff, "w", driver="GTiff",
            height=grid.shape[0], width=grid.shape[1],
            count=1, dtype=grid.dtype,
            crs=RioCRS.from_epsg(4326), transform=transform
        ) as dst:
            dst.write(grid, 1)
    else:
        # True Color → 3 bandes
        nrows, ncols = lon_2d.shape
        bands = []
        for c in range(3):
            vals = data[:, :, c].ravel()
            mask_flat = np.isnan(vals)
            g = griddata(points[~mask_flat],
                         vals[~mask_flat],
                         (lon_2d, lat_2d), method="linear")
            bands.append(g)
        transform = from_bounds(lon_min, lat_min, lon_max, lat_max,
                                ncols, nrows)
        with rasterio.open(
            out_path_tiff, "w", driver="GTiff",
            height=nrows, width=ncols,
            count=3, dtype=np.float32,
            crs=RioCRS.from_epsg(4326), transform=transform
        ) as dst:
            for i, b in enumerate(bands, start=1):
                dst.write(b.astype(np.float32), i)

    print(f"[OUT]  GeoTIFF écrit : {out_path_tiff}")


# ── Rendu principal ────────────────────────────────────────────────────────────

def render_truecolor(sdr_files, geo_file, out_path, meta,
                     destripe_flag=False, geotiff=False):
    """
    Rendu True Color (RGB) géoréférencé.
    sdr_files : liste de 3 fichiers HDF5 [I1=rouge, I2=vert, I3=bleu]
    """
    if len(sdr_files) < 3:
        raise ValueError("True Color nécessite 3 fichiers SDR (I1, I2, I3)")

    print("[TC]   Lecture géolocalisation...")
    lat, lon = read_geo(geo_file, band_type="I")

    print("[TC]   Lecture bandes radiométriques...")
    r = read_reflectance_band(sdr_files[0], "I1 rouge  0.64µm")
    g = read_reflectance_band(sdr_files[1], "I2 vert   0.86µm")
    b = read_reflectance_band(sdr_files[2], "I3 bleu   0.47µm")

    if destripe_flag:
        r = destripe(r)
        g = destripe(g)
        b = destripe(b)

    print("[TC]   Construction RGB...")
    rgb = build_rgb(r, g, b, gamma=0.5)

    _render(rgb, lat, lon, out_path, meta, mode="truecolor", geotiff=geotiff)


def render_thermal(sdr_file, geo_file, out_path, meta,
                   destripe_flag=False, geotiff=False):
    """
    Rendu canal thermique (M15) en température de brillance.
    """
    print("[TH]   Lecture géolocalisation...")
    lat, lon = read_geo(geo_file, band_type="M")

    print("[TH]   Lecture bande M15 (10.76µm)...")
    bt = read_radiance_band(sdr_file)

    if destripe_flag:
        bt = destripe(bt)

    _render(bt, lat, lon, out_path, meta, mode="thermal", geotiff=geotiff)


def _render(data, lat, lon, out_path, meta, mode, geotiff):
    """
    Moteur de rendu commun — projection Cartopy, côtes, grille, annotations.
    """
    lat_min, lat_max = float(lat.min()), float(lat.max())
    lon_min, lon_max = float(lon.min()), float(lon.max())
    margin = 1.5

    # ── Figure et axes Cartopy ──
    if HAS_CARTOPY:
        proj = ccrs.PlateCarree(central_longitude=(lon_min + lon_max) / 2)
        fig  = plt.figure(figsize=(20, 7), dpi=150)
        ax   = fig.add_subplot(1, 1, 1, projection=proj)
        ax.set_extent([lon_min - margin, lon_max + margin,
                       lat_min - margin, lat_max + margin],
                      crs=ccrs.PlateCarree())
    else:
        fig, ax = plt.subplots(figsize=(20, 7), dpi=150)

    # ── Tracé des données ──
    if mode == "truecolor":
        if HAS_CARTOPY:
            # pcolormesh sur grille curviligne
            # Pour le RGB, tracer canal par canal en RGBA
            # Méthode : créer une image RGBA et la placer avec imshow + extent
            # (pcolormesh natif RGB n'est pas supporté par Cartopy)
            # → fallback imshow avec extent bounding box
            ax.imshow(
                np.clip(data, 0, 1),
                origin="upper",
                extent=[lon_min, lon_max, lat_min, lat_max],
                transform=ccrs.PlateCarree(),
                interpolation="bilinear",
                aspect="auto",
                zorder=1
            )
        else:
            ax.imshow(np.clip(data, 0, 1), origin="upper", aspect="auto")

    elif mode == "thermal":
        # Colormap : bleu (froid = sommets nuageux) → rouge (chaud = surface)
        cmap = plt.cm.RdYlBu_r
        vmin, vmax = 210, 305  # K — plage typique Caraïbes
        if HAS_CARTOPY:
            im = ax.pcolormesh(
                lon, lat, data,
                transform=ccrs.PlateCarree(),
                cmap=cmap, vmin=vmin, vmax=vmax,
                shading="auto", zorder=1
            )
        else:
            im = ax.pcolormesh(lon, lat, data,
                               cmap=cmap, vmin=vmin, vmax=vmax,
                               shading="auto")
        # Colorbar avec axe secondaire °C
        cbar = fig.colorbar(im, ax=ax, fraction=0.02, pad=0.02)
        cbar.set_label("Température de brillance", fontsize=9)
        # Axe secondaire °C
        cbar2 = cbar.ax.twinx()
        cbar2.set_ylim(vmin - 273.15, vmax - 273.15)
        cbar2.set_ylabel("°C", fontsize=8)

    # ── Couches géographiques ──
    if HAS_CARTOPY:
        ax.coastlines(resolution="10m", color="white",
                      linewidth=0.8, zorder=5)
        ax.add_feature(cfeature.BORDERS, linestyle="--",
                       edgecolor="yellow", linewidth=0.6, zorder=5)
        ax.add_feature(cfeature.LAKES, alpha=0.2, zorder=4)

        # Grille lat/lon
        gl = ax.gridlines(
            crs=ccrs.PlateCarree(),
            draw_labels=True,
            linewidth=0.4, color="white", alpha=0.6,
            linestyle="--", zorder=6
        )
        gl.top_labels    = False
        gl.right_labels  = False
        gl.xformatter    = LONGITUDE_FORMATTER
        gl.yformatter    = LATITUDE_FORMATTER
        gl.xlabel_style  = {"size": 8, "color": "white"}
        gl.ylabel_style  = {"size": 8, "color": "white"}

        # Espacement automatique de la grille
        span_lon = lon_max - lon_min
        step     = 10 if span_lon > 40 else (5 if span_lon > 20 else 2)
        gl.xlocator = mticker.FixedLocator(
            np.arange(np.floor(lon_min), np.ceil(lon_max) + step, step))
        gl.ylocator = mticker.FixedLocator(
            np.arange(np.floor(lat_min), np.ceil(lat_max) + step, step))

        # Annotations POI
        annotate_pois(ax, lat, lon, proj)

    # ── Titre ──
    mode_label = "True Color (I1/I2/I3)" if mode == "truecolor" \
                 else "Canal thermique — M15 (10.76µm)"
    orbit_str = f"Orbite #{meta.get('orbit_number', '?')}"
    title = (f"NOAA-20 / VIIRS  —  {mode_label}\n"
             f"{meta.get('datetime_str', 'date inconnue')}  ·  {orbit_str}  "
             f"·  Granule {meta.get('granule_id', '?')}")
    ax.set_title(title, fontsize=9, color="white", pad=8)
    fig.patch.set_facecolor("#0a0a0a")
    ax.set_facecolor("#0a0a0a")

    # ── Sauvegarde PNG ──
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=300, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"[OUT]  PNG écrit : {out_path}")

    # ── GeoTIFF optionnel ──
    if geotiff:
        save_geotiff(data, lat, lon, out_path, mode)

    # ── JSON de métadonnées ──
    meta_out = {
        "granule_id":    meta.get("granule_id", "unknown"),
        "datetime_utc":  meta.get("datetime_str", "unknown"),
        "orbit_number":  int(meta.get("orbit_number", 0)),
        "mode":          mode,
        "bbox": {
            "lat_min": round(lat_min, 4),
            "lat_max": round(lat_max, 4),
            "lon_min": round(lon_min, 4),
            "lon_max": round(lon_max, 4),
        },
        "swath_width_km": round((lon_max - lon_min) * 111.0, 0),
        "output_png": str(out_path),
    }
    json_path = out_path.with_suffix(".json")
    with open(json_path, "w") as f:
        json.dump(meta_out, f, indent=2)
    print(f"[OUT]  JSON écrit : {json_path}")
    print(f"[OK]   Bbox : {lat_min:.2f}°N {lon_min:.2f}°E  "
          f"→  {lat_max:.2f}°N {lon_max:.2f}°E")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Visualisation géoréférencée de granules VIIRS Level 1 (IPOPP)"
    )
    parser.add_argument("--sdr", nargs="+", required=True,
                        help="Fichier(s) HDF5 SDR VIIRS (1 pour thermal, 3 pour truecolor)")
    parser.add_argument("--geo", required=True,
                        help="Fichier HDF5 de géolocalisation (GIGTO ou GMODO)")
    parser.add_argument("--mode", choices=["truecolor", "thermal"],
                        default="truecolor",
                        help="Mode de rendu (défaut : truecolor)")
    parser.add_argument("--out", default="viirs_output.png",
                        help="Chemin du fichier PNG de sortie")
    parser.add_argument("--destripe", action="store_true",
                        help="Appliquer la correction de striping inter-détecteurs")
    parser.add_argument("--geotiff", action="store_true",
                        help="Produire également un GeoTIFF (nécessite rasterio + scipy)")
    args = parser.parse_args()

    if not HAS_CARTOPY:
        print("[WARN] cartopy absent — installer avec : pip install cartopy")

    # Lecture des métadonnées depuis le premier fichier SDR
    print(f"[META] Lecture métadonnées depuis {args.sdr[0]}")
    try:
        meta = read_viirs_metadata(args.sdr[0])
        print(f"[META] Granule : {meta['granule_id']}")
        print(f"[META] Date    : {meta['datetime_str']}")
        print(f"[META] Orbite  : #{meta['orbit_number']}")
    except Exception as e:
        print(f"[WARN] Métadonnées non disponibles : {e}")
        meta = {}

    if args.mode == "truecolor":
        render_truecolor(
            sdr_files    = args.sdr,
            geo_file     = args.geo,
            out_path     = args.out,
            meta         = meta,
            destripe_flag= args.destripe,
            geotiff      = args.geotiff,
        )
    else:
        render_thermal(
            sdr_file     = args.sdr[0],
            geo_file     = args.geo,
            out_path     = args.out,
            meta         = meta,
            destripe_flag= args.destripe,
            geotiff      = args.geotiff,
        )


if __name__ == "__main__":
    main()
