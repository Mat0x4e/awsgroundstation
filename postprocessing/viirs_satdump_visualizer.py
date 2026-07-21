#!/usr/bin/env python3
"""
viirs_satdump_visualizer.py
===========================
Visualisation géoréférencée de produits VIIRS sortis de SatDump.

SatDump produit un dossier par passe contenant :
  - products/VIIRS/product.cbor  — métadonnées calibration + projection + timestamps
  - products/VIIRS/<channel>.png — images par canal (16 bits ou 8 bits selon config)
  - products/VIIRS/<channel>.georef (optionnel) — coordonnées lat/lon pour certaines builds

Ce script lit les PNG 16 bits de SatDump et les coordonnées CBOR pour produire
une image géoréférencée avec côtes, frontières et grille lat/lon — équivalent au
script viirs_georef_visualizer.py pour la chaîne RT-STPS/CSPP SDR.

Usage :
  python viirs_satdump_visualizer.py \
      --satdump_dir /path/to/satdump/output/2024-06-10_14-00_noaa_20_viirs/ \
      --mode truecolor \
      --out caribbean_satdump_truecolor.png

  python viirs_satdump_visualizer.py \
      --satdump_dir /path/to/satdump/output/2024-06-10_14-00_noaa_20_viirs/ \
      --mode thermal \
      --channel M15 \
      --out caribbean_satdump_thermal.png

Différences clés vs viirs_georef_visualizer.py (chaîne CSPP SDR) :
  - Entrée PNG (SatDump) au lieu de HDF5 (CSPP SDR)
  - Métadonnées dans product.cbor (CBOR) au lieu de HDF5 attributes
  - Géolocalisation approximative TLE-based (SatDump) vs corrections terrain CPM (CSPP)
  - Calibration communautaire (SatDump) vs algorithmique officielle NOAA (CSPP)
"""

import argparse
import json
import os
import struct
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import numpy.ma as ma
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

try:
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from cartopy.mpl.gridliner import LONGITUDE_FORMATTER, LATITUDE_FORMATTER
    HAS_CARTOPY = True
except ImportError:
    HAS_CARTOPY = False
    print("[WARN] cartopy non disponible — pip install cartopy")

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("[WARN] Pillow non disponible — pip install Pillow")

try:
    import cbor2
    HAS_CBOR = True
except ImportError:
    HAS_CBOR = False
    print("[WARN] cbor2 non disponible — pip install cbor2")

warnings.filterwarnings("ignore", category=UserWarning)

# ── Points d'intérêt Caraïbes + Pacifique ────────────────────────────────────
POI_LABELS = {
    "Jamaïque":         (17.9,  -77.3),
    "Cuba":             (21.5,  -79.5),
    "Haïti":            (18.9,  -72.7),
    "Rép. Dominicaine": (18.7,  -70.2),
    "Porto Rico":       (18.2,  -66.5),
    "Yucatán":          (20.5,  -89.0),
    "Floride":          (27.5,  -81.5),
    "Hawaï":            (20.5, -157.0),
    "Californie":       (37.0, -120.0),
    "Mexique":          (20.0, -105.0),
}

# ── Noms de canaux SatDump → numéro de bande VIIRS ───────────────────────────
# SatDump nomme les fichiers PNG selon le nom de bande (ex: "M15", "I1", "DNB")
SATDUMP_TRUECOLOR_CHANNELS = {
    "red":   ["I1", "M5"],     # rouge — I1 préféré, M5 fallback
    "green": ["I2", "M7"],     # vert
    "blue":  ["I3", "M3"],     # bleu
}
SATDUMP_THERMAL_CHANNELS = ["M15", "M16", "I5"]


def read_cbor_metadata(cbor_path):
    """
    Lit le fichier product.cbor de SatDump.
    Retourne un dict avec timestamps, satellite, projection info.
    Si cbor2 n'est pas disponible, retourne un dict vide avec avertissement.
    """
    meta = {
        "satellite": "NOAA-20",
        "instrument": "VIIRS",
        "datetime_str": "date inconnue",
        "corner_coords": None,
    }

    if not HAS_CBOR:
        print("[WARN] cbor2 absent — métadonnées CBOR non lues")
        print("       Installer : pip install cbor2")
        return meta

    try:
        with open(cbor_path, "rb") as f:
            data = cbor2.load(f)

        # SatDump CBOR structure (approximative — varie selon la version)
        if isinstance(data, dict):
            # Timestamp
            ts = data.get("timestamp") or data.get("start_timestamp")
            if ts:
                try:
                    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                    meta["datetime_str"] = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                except Exception:
                    pass

            # Satellite
            sat = data.get("satellite") or data.get("sat_name")
            if sat:
                meta["satellite"] = str(sat)

            # Projection / corner coordinates
            proj = data.get("projection") or data.get("geo_correction")
            if proj and isinstance(proj, dict):
                meta["corner_coords"] = proj

        print(f"[CBOR] Satellite : {meta['satellite']}")
        print(f"[CBOR] Date      : {meta['datetime_str']}")

    except Exception as e:
        print(f"[WARN] Lecture CBOR partielle : {e}")

    return meta


def find_channel_file(satdump_dir, candidates):
    """
    Cherche le premier fichier PNG disponible parmi une liste de noms candidats.
    SatDump peut nommer les fichiers différemment selon la version et la config.
    """
    satdump_dir = Path(satdump_dir)
    # Chercher dans le dossier et ses sous-dossiers immédiats
    search_dirs = [satdump_dir] + list(satdump_dir.glob("*/"))

    for name in candidates:
        for d in search_dirs:
            for pattern in [f"{name}.png", f"{name.lower()}.png",
                            f"VIIRS_{name}.png", f"VIIRS-{name}.png",
                            f"channel_{name}.png"]:
                path = d / pattern
                if path.exists():
                    print(f"[FILE] {name} → {path}")
                    return path

    return None


def load_png_channel(png_path, normalize=True):
    """
    Charge un canal PNG de SatDump.
    SatDump peut produire des PNG 16 bits (valeurs physiques) ou 8 bits (affichage).
    Retourne un tableau float32 numpy, masqué sur les pixels noirs (fill).
    """
    if not HAS_PILLOW:
        raise RuntimeError("Pillow requis — pip install Pillow")

    img = Image.open(png_path)
    arr = np.array(img, dtype=np.float32)

    print(f"[PNG]  {png_path.name} : shape={arr.shape} mode={img.mode} "
          f"min={arr.min():.1f} max={arr.max():.1f}")

    # PNG 16 bits → normaliser en [0, 1]
    if img.mode == "I;16" or arr.max() > 255:
        arr = arr / 65535.0
    elif normalize:
        arr = arr / 255.0

    # Masquer les pixels noirs (no-data dans SatDump)
    arr_ma = ma.masked_where(arr < 1e-6, arr)
    return arr_ma


def build_geo_from_tle(satdump_dir, cbor_meta):
    """
    Construit une grille lat/lon approximative à partir du CBOR SatDump.
    SatDump stocke les coordonnées des coins du swath dans le CBOR.
    Si pas disponible, utilise une grille fictive centrée sur le bounding box.

    Retourne (lat_2d, lon_2d) en tableaux numpy ou None si impossible.
    """
    # Essayer de lire un fichier .georef s'il existe (certaines versions SatDump)
    satdump_dir = Path(satdump_dir)
    georef_files = list(satdump_dir.glob("**/*.georef"))

    if georef_files:
        try:
            with open(georef_files[0]) as f:
                georef = json.load(f)
            print(f"[GEO]  Fichier .georef trouvé : {georef_files[0].name}")
            # Format .georef SatDump : {"top_left": [lat, lon], "top_right": ...}
            corners = [
                georef.get("top_left", [0, 0]),
                georef.get("top_right", [0, 0]),
                georef.get("bottom_left", [0, 0]),
                georef.get("bottom_right", [0, 0]),
            ]
            lats = [c[0] for c in corners]
            lons = [c[1] for c in corners]
            print(f"[GEO]  Bounding box : lat {min(lats):.1f}→{max(lats):.1f}  "
                  f"lon {min(lons):.1f}→{max(lons):.1f}")
            return min(lats), max(lats), min(lons), max(lons)
        except Exception as e:
            print(f"[WARN] Lecture .georef : {e}")

    # Fallback : bounding box approximative depuis le CBOR
    if cbor_meta.get("corner_coords"):
        print("[GEO]  Coordonnées depuis CBOR")
        cc = cbor_meta["corner_coords"]
        if isinstance(cc, dict):
            lats = [v for k, v in cc.items() if "lat" in k.lower()]
            lons = [v for k, v in cc.items() if "lon" in k.lower()]
            if lats and lons:
                return min(lats), max(lats), min(lons), max(lons)

    print("[WARN] Aucune géolocalisation disponible — bounding box non déterminée")
    return None


def gamma_stretch(band, gamma=0.5, p_low=2, p_high=98):
    """Correction gamma + étirement par percentiles."""
    valid = band.compressed() if ma.is_masked(band) else band.ravel()
    if len(valid) == 0:
        return band
    vmin = np.percentile(valid, p_low)
    vmax = np.percentile(valid, p_high)
    stretched = ma.clip((band - vmin) / (vmax - vmin + 1e-10), 0.0, 1.0)
    return stretched ** gamma


def annotate_pois(ax, lat_min, lat_max, lon_min, lon_max):
    """Annote les POI visibles dans le swath."""
    margin = 2.0
    for name, (poi_lat, poi_lon) in POI_LABELS.items():
        if (lat_min - margin <= poi_lat <= lat_max + margin and
                lon_min - margin <= poi_lon <= lon_max + margin):
            ax.text(
                poi_lon, poi_lat, name,
                transform=ccrs.PlateCarree(),
                fontsize=7, color="white", ha="center", va="center",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="black",
                          alpha=0.45, edgecolor="none"),
                zorder=10
            )


def render(data, bbox, out_path, meta, mode, is_rgb=False):
    """Moteur de rendu commun — Cartopy + couches géo."""
    lat_min, lat_max, lon_min, lon_max = bbox
    margin = 1.5

    if HAS_CARTOPY:
        proj = ccrs.PlateCarree(central_longitude=(lon_min + lon_max) / 2)
        fig  = plt.figure(figsize=(20, 7), dpi=150)
        ax   = fig.add_subplot(1, 1, 1, projection=proj)
        ax.set_extent([lon_min - margin, lon_max + margin,
                       lat_min - margin, lat_max + margin],
                      crs=ccrs.PlateCarree())
    else:
        fig, ax = plt.subplots(figsize=(20, 7), dpi=150)

    extent = [lon_min, lon_max, lat_min, lat_max]

    if is_rgb:
        ax.imshow(np.clip(data, 0, 1), origin="upper", extent=extent,
                  transform=ccrs.PlateCarree() if HAS_CARTOPY else None,
                  interpolation="bilinear", aspect="auto", zorder=1)
    else:
        # Thermique
        cmap = plt.cm.RdYlBu_r
        vmin, vmax = 0.0, 1.0  # SatDump normalise en [0,1]
        im = ax.imshow(data, origin="upper", extent=extent,
                       transform=ccrs.PlateCarree() if HAS_CARTOPY else None,
                       cmap=cmap, vmin=vmin, vmax=vmax,
                       interpolation="bilinear", aspect="auto", zorder=1)
        cbar = fig.colorbar(im, ax=ax, fraction=0.02, pad=0.02)
        cbar.set_label("Valeur normalisée SatDump\n(converti en unités physiques\npar calibration SatDump)", fontsize=8)

    if HAS_CARTOPY:
        ax.coastlines(resolution="10m", color="white", linewidth=0.8, zorder=5)
        ax.add_feature(cfeature.BORDERS, linestyle="--",
                       edgecolor="yellow", linewidth=0.6, zorder=5)
        ax.add_feature(cfeature.LAKES, alpha=0.2, zorder=4)
        gl = ax.gridlines(crs=ccrs.PlateCarree(), draw_labels=True,
                          linewidth=0.4, color="white", alpha=0.6,
                          linestyle="--", zorder=6)
        gl.top_labels   = False
        gl.right_labels = False
        gl.xformatter   = LONGITUDE_FORMATTER
        gl.yformatter   = LATITUDE_FORMATTER
        gl.xlabel_style = {"size": 8, "color": "white"}
        gl.ylabel_style = {"size": 8, "color": "white"}
        span_lon = lon_max - lon_min
        step = 10 if span_lon > 40 else (5 if span_lon > 20 else 2)
        gl.xlocator = mticker.FixedLocator(
            np.arange(np.floor(lon_min), np.ceil(lon_max) + step, step))
        gl.ylocator = mticker.FixedLocator(
            np.arange(np.floor(lat_min), np.ceil(lat_max) + step, step))
        annotate_pois(ax, lat_min, lat_max, lon_min, lon_max)

    mode_label = "True Color (I1/I2/I3)" if mode == "truecolor" else "Thermique (M15)"
    title = (f"NOAA-20 / VIIRS  —  {mode_label}  [SatDump]\n"
             f"{meta.get('datetime_str', 'date inconnue')}  "
             f"·  Calibration communautaire (non traçable NOAA)")
    ax.set_title(title, fontsize=9, color="white", pad=8)
    # Avertissement calibration
    fig.text(0.01, 0.02,
             "⚠ Calibration SatDump : réimplémentation communautaire — "
             "LUT statiques — usage exploratoire, non certifié NOAA",
             fontsize=7, color="#FCA5A5", ha="left")
    fig.patch.set_facecolor("#0a0a0a")
    ax.set_facecolor("#0a0a0a")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=300, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"[OUT]  PNG écrit : {out_path}")

    # JSON métadonnées
    json_out = {
        "source": "SatDump",
        "satellite": meta.get("satellite", "NOAA-20"),
        "datetime_utc": meta.get("datetime_str", "unknown"),
        "mode": mode,
        "bbox": {"lat_min": lat_min, "lat_max": lat_max,
                 "lon_min": lon_min, "lon_max": lon_max},
        "calibration_note": "Communautaire SatDump — non certifiée NOAA",
        "output_png": str(out_path),
    }
    with open(out_path.with_suffix(".json"), "w") as f:
        json.dump(json_out, f, indent=2)
    print(f"[OUT]  JSON : {out_path.with_suffix('.json')}")


def main():
    parser = argparse.ArgumentParser(
        description="Visualisation géoréférencée depuis produits SatDump VIIRS"
    )
    parser.add_argument("--satdump_dir", required=True,
                        help="Dossier de sortie SatDump pour une passe VIIRS")
    parser.add_argument("--mode", choices=["truecolor", "thermal"],
                        default="truecolor")
    parser.add_argument("--channel", default="M15",
                        help="Canal thermique à utiliser (défaut: M15)")
    parser.add_argument("--out", default="satdump_viirs_output.png")
    parser.add_argument("--bbox", nargs=4, type=float,
                        metavar=("LAT_MIN", "LAT_MAX", "LON_MIN", "LON_MAX"),
                        help="Bounding box manuelle si pas de géoloc dans les fichiers")
    args = parser.parse_args()

    satdump_dir = Path(args.satdump_dir)
    if not satdump_dir.exists():
        print(f"[ERR] Dossier introuvable : {satdump_dir}")
        return

    # ── Lecture CBOR ──
    cbor_path = satdump_dir / "product.cbor"
    if not cbor_path.exists():
        # Chercher dans les sous-dossiers
        cbor_files = list(satdump_dir.glob("**/product.cbor"))
        cbor_path = cbor_files[0] if cbor_files else None

    meta = read_cbor_metadata(cbor_path) if cbor_path else {}

    # ── Bounding box ──
    if args.bbox:
        bbox = tuple(args.bbox)
        print(f"[GEO]  Bounding box manuelle : {bbox}")
    else:
        result = build_geo_from_tle(satdump_dir, meta)
        if result:
            bbox = result
        else:
            print("[ERR] Aucune géolocalisation disponible.")
            print("      Utilisez --bbox LAT_MIN LAT_MAX LON_MIN LON_MAX")
            print("      Exemple Caraïbes : --bbox 10 35 -95 -60")
            return

    # ── Chargement des canaux ──
    if args.mode == "truecolor":
        channels = {}
        for color, candidates in SATDUMP_TRUECOLOR_CHANNELS.items():
            path = find_channel_file(satdump_dir, candidates)
            if path is None:
                print(f"[ERR] Canal {color} ({candidates}) introuvable dans {satdump_dir}")
                print("      Vérifiez le contenu du dossier SatDump")
                return
            channels[color] = load_png_channel(path)

        # Construire RGB — crop to minimum common dimensions
        H = min(channels["red"].shape[0], channels["green"].shape[0], channels["blue"].shape[0])
        W = min(channels["red"].shape[1], channels["green"].shape[1], channels["blue"].shape[1])
        channels = {k: v[:H, :W] for k, v in channels.items()}
        rgb = np.full((H, W, 3), np.nan, dtype=np.float32)
        mask = (ma.getmaskarray(channels["red"]) |
                ma.getmaskarray(channels["green"]) |
                ma.getmaskarray(channels["blue"]))
        for idx, c in enumerate(["red", "green", "blue"]):
            ch = gamma_stretch(channels[c], gamma=0.5)
            rgb[~mask, idx] = ch.data[~mask]

        render(rgb, bbox, args.out, meta, "truecolor", is_rgb=True)

    else:  # thermal
        candidates = [args.channel] + [c for c in SATDUMP_THERMAL_CHANNELS
                                        if c != args.channel]
        path = find_channel_file(satdump_dir, candidates)
        if path is None:
            print(f"[ERR] Canal thermique introuvable ({candidates})")
            return
        data = load_png_channel(path, normalize=True)
        render(data, bbox, args.out, meta, "thermal", is_rgb=False)


if __name__ == "__main__":
    main()
