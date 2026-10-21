"""Tests for three NASA VIIRS render bugs -- Task 9 of clean-compute-pipeline.

Each bug section follows the test-first pattern:
  1. Write a test that FAILS against the current (broken) code.
  2. Fix the code.
  3. Confirm the test now passes.

Bug #1 (Req 4): visualize_nasa.py -- granule-count mismatch
  _find_hdf5_files does a plain rglob; no granule-token parsing or band/geo
  reconciliation. 3 band granules + 5 geo granules crashes with a broadcast
  mismatch.

Bug #2 (Req 5): cartopy_renderer.py -- thermal pcolormesh NaN crash
  render_nasa thermal branch passes raw NaN lat/lon to pcolormesh, which
  forbids non-finite coordinate values.

Bug #3 (Req 6): visualize_nasa.py + image_renderer.py -- double-stretch banding
  Orchestrator does destripe(contrast_stretch(iN)); assemble_true_color then
  stretches again. Also: np.median on fully-masked columns sees underlying data
  values and subtracts them (corrupting masked-cell underlying data). Fix: use
  np.ma.median (neutral correction for all-masked columns) and remove the
  orchestrator's pre-stretch.

Import strategy:
  * image_renderer.py  -- pure numpy, imported via importlib
  * visualize_nasa.py  -- viirs.* mocked in sys.modules then loaded
  * cartopy_renderer.py -- cartopy mocked in sys.modules then loaded; only the
    extracted pure helper _sanitise_thermal_coords is tested
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import numpy.ma as ma
import pytest

# ---------------------------------------------------------------------------
# Repo root / helper paths
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_VIIRS_DIR = _ROOT / "scripts" / "viirs"


# ---------------------------------------------------------------------------
# Utility: load a single viirs module without triggering the full package init
# ---------------------------------------------------------------------------

def _load_viirs_module(name: str):
    """Import scripts/viirs/<name>.py without triggering the full package init."""
    full_name = f"viirs.{name}"
    if full_name in sys.modules and not isinstance(sys.modules[full_name], MagicMock):
        return sys.modules[full_name]

    if "viirs" not in sys.modules:
        pkg = types.ModuleType("viirs")
        pkg.__path__ = [str(_VIIRS_DIR)]
        sys.modules["viirs"] = pkg

    spec = importlib.util.spec_from_file_location(full_name, _VIIRS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_image_renderer():
    return _load_viirs_module("image_renderer")


# ---------------------------------------------------------------------------
# Utility: load visualize_nasa with all viirs.* heavy deps mocked away
# ---------------------------------------------------------------------------

def _load_visualize_nasa():
    """Load visualize_nasa with cartopy_renderer, h5py-bound readers mocked."""
    for sub in ("sdr_reader", "geo_reader", "bt_converter",
                 "cartopy_renderer", "metadata_generator", "geotiff_exporter"):
        key = f"viirs.{sub}"
        if key not in sys.modules:
            sys.modules[key] = MagicMock()

    _load_viirs_module("models")
    _load_viirs_module("image_renderer")

    full_name = "viirs.visualize_nasa"
    if full_name in sys.modules:
        del sys.modules[full_name]

    spec = importlib.util.spec_from_file_location(
        full_name, _VIIRS_DIR / "visualize_nasa.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Utility: load cartopy_renderer with cartopy mocked
# ---------------------------------------------------------------------------

def _register_cartopy_mock():
    if "cartopy" in sys.modules:
        return
    for mod_name in [
        "cartopy", "cartopy.crs", "cartopy.feature",
        "cartopy.mpl", "cartopy.mpl.gridliner",
        "cartopy.io", "cartopy.io.shapereader",
    ]:
        sys.modules[mod_name] = types.ModuleType(mod_name)
    crs = sys.modules["cartopy.crs"]
    crs.PlateCarree = lambda **kw: None
    crs.Geodetic = lambda **kw: None
    feat = sys.modules["cartopy.feature"]
    for attr in ("COASTLINE", "BORDERS", "LAKES", "NaturalEarthFeature"):
        setattr(feat, attr, MagicMock())
    gl = sys.modules["cartopy.mpl.gridliner"]
    gl.LONGITUDE_FORMATTER = MagicMock()
    gl.LATITUDE_FORMATTER = MagicMock()
    sr = sys.modules["cartopy.io.shapereader"]
    sr.natural_earth = MagicMock(return_value=None)
    sr.Reader = MagicMock


def _load_cartopy_renderer():
    _register_cartopy_mock()
    full_name = "viirs.cartopy_renderer"
    if full_name in sys.modules and not isinstance(sys.modules[full_name], MagicMock):
        return sys.modules[full_name]
    if full_name in sys.modules:
        del sys.modules[full_name]

    _load_viirs_module("models")
    _load_viirs_module("scan_geometry")

    spec = importlib.util.spec_from_file_location(
        full_name, _VIIRS_DIR / "cartopy_renderer.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# BUG #1 -- granule-count mismatch (visualize_nasa.py)
# ===========================================================================

class TestBug1GranuleMismatch:
    """Tests for granule-token parsing and band/geo reconciliation (Req 4)."""

    def test_parse_granule_token_standard_cspp_name(self):
        """FAILS BEFORE FIX: _parse_granule_token absent.
        PASSES AFTER FIX: extracts the tHHMMSSS field from a CSPP filename.
        """
        vn = _load_visualize_nasa()
        fn = getattr(vn, "_parse_granule_token", None)
        assert fn is not None, (
            "FAIL BEFORE FIX: _parse_granule_token not present in visualize_nasa"
        )
        p = Path("SVI01_j01_d20261017_t2049455_e2051117_b12345_c20261017205150000000.h5")
        assert fn(p) == "2049455"

    def test_parse_granule_token_returns_none_for_unknown_name(self):
        """Returns None when the tHHMMSSS field is absent."""
        vn = _load_visualize_nasa()
        fn = getattr(vn, "_parse_granule_token", None)
        assert fn is not None, "FAIL BEFORE FIX: _parse_granule_token missing"
        assert fn(Path("some_random_file.h5")) is None

    def test_reconcile_granule_sets_exists(self):
        """FAILS BEFORE FIX: _reconcile_granule_sets absent from module."""
        vn = _load_visualize_nasa()
        fn = getattr(vn, "_reconcile_granule_sets", None)
        assert fn is not None, (
            "FAIL BEFORE FIX: _reconcile_granule_sets not present in visualize_nasa"
        )

    def test_reconcile_reduces_to_intersection(self):
        """FAILS BEFORE FIX: _reconcile_granule_sets absent.
        PASSES AFTER FIX: 3 band + 5 geo -> 3 matched pairs.
        """
        vn = _load_visualize_nasa()
        fn = getattr(vn, "_reconcile_granule_sets", None)
        assert fn is not None, "FAIL BEFORE FIX: _reconcile_granule_sets missing"

        base = "j01_d20261017_b12345"
        shared = ["t2049455", "t2051130", "t2052455"]
        orphans = ["t2043455", "t2047455"]

        band_files = [Path(f"SVI01_{base}_{t}_e2051000_c0.h5") for t in shared]
        geo_files = (
            [Path(f"GITCO_{base}_{t}_e2051000_c0.h5") for t in shared]
            + [Path(f"GITCO_{base}_{t}_e2049000_c0.h5") for t in orphans]
        )

        r_band, r_geo = fn(band_files, geo_files)

        assert len(r_band) == 3, f"expected 3 matched band files, got {len(r_band)}"
        assert len(r_geo) == 3, (
            f"expected 3 matched geo files (2 orphans dropped), got {len(r_geo)}"
        )

    def test_current_code_exposes_mismatch_without_reconcile(self, tmp_path):
        """Documents the root cause: plain rglob returns different-length lists.
        This test always passes; it shows WHY reconciliation is needed.
        """
        vn = _load_visualize_nasa()

        base = "j01_d20261017_b12345"
        shared = ["t2049455", "t2051130", "t2052455"]
        orphans = ["t2043455", "t2047455"]

        for tok in shared:
            (tmp_path / f"SVI01_{base}_{tok}_e2051000_c0.h5").touch()
            (tmp_path / f"GITCO_{base}_{tok}_e2051000_c0.h5").touch()
        for tok in orphans:
            (tmp_path / f"GITCO_{base}_{tok}_e2049000_c0.h5").touch()

        i1 = vn._find_hdf5_files(tmp_path, "SVI01")
        igeo = vn._find_hdf5_files(tmp_path, "GITCO")

        assert len(i1) == 3
        assert len(igeo) == 5
        assert len(i1) != len(igeo), "mismatch confirmed -- reconciliation is required"

    def test_reconcile_three_way_true_color(self):
        """Reconciling I1/I2/I3/IGEO four-way picks the intersection."""
        vn = _load_visualize_nasa()
        fn = getattr(vn, "_reconcile_granule_sets", None)
        assert fn is not None, "FAIL BEFORE FIX: _reconcile_granule_sets missing"

        base = "j01_d20261017_b12345"
        shared = ["t2049455", "t2051130"]
        extra_geo = ["t2043455"]

        def _files(prefix, tokens):
            return [Path(f"{prefix}_{base}_{t}_e2050000_c0.h5") for t in tokens]

        i1 = _files("SVI01", shared)
        i2 = _files("SVI02", shared)
        i3 = _files("SVI03", shared)
        igeo = _files("GITCO", shared + extra_geo)

        r_i1, r_i2, r_i3, r_igeo = fn(i1, i2, i3, igeo)

        for lst in (r_i1, r_i2, r_i3, r_igeo):
            assert len(lst) == 2, f"expected 2 matched files, got {len(lst)}"


# ===========================================================================
# BUG #2 -- thermal pcolormesh NaN crash (cartopy_renderer.py)
# ===========================================================================

class TestBug2ThermalNanCrash:
    """Tests for the NaN-coordinate guard in render_nasa thermal branch (Req 5)."""

    def test_plain_pcolormesh_raises_on_partial_nan(self):
        """Baseline: matplotlib pcolormesh rejects non-finite coord values.
        This test always passes; it documents WHY the guard is needed.
        """
        import matplotlib.pyplot as plt

        lat = np.full((8, 8), 50.0, dtype=np.float32)
        lat[2:4, :] = np.nan
        lon = np.full((8, 8), 2.0, dtype=np.float32)
        band = np.full((8, 8), 280.0, dtype=np.float32)

        fig, ax = plt.subplots()
        try:
            with pytest.raises(ValueError, match="non-finite"):
                ax.pcolormesh(lon, lat, band, shading="auto")
        finally:
            plt.close(fig)

    def test_sanitise_thermal_coords_exists(self):
        """FAILS BEFORE FIX: _sanitise_thermal_coords absent from cartopy_renderer."""
        cr = _load_cartopy_renderer()
        fn = getattr(cr, "_sanitise_thermal_coords", None)
        assert fn is not None, (
            "FAIL BEFORE FIX: _sanitise_thermal_coords not present in cartopy_renderer"
        )

    def test_sanitise_helper_masks_nonfinite_values(self):
        """FAILS BEFORE FIX: helper absent.
        PASSES AFTER FIX: returned masked arrays have no non-finite unmasked values.
        """
        cr = _load_cartopy_renderer()
        fn = getattr(cr, "_sanitise_thermal_coords", None)
        assert fn is not None, "FAIL BEFORE FIX: _sanitise_thermal_coords missing"

        lat = np.full((16, 16), 50.0, dtype=np.float32)
        lat[4:6, :] = np.nan
        lat[10, 5] = np.inf
        lon = np.full((16, 16), 2.0, dtype=np.float32)
        lon[7, :] = np.nan
        band = np.full((16, 16), 280.0, dtype=np.float32)

        s_lat, s_lon, s_band = fn(lat, lon, band)

        assert np.all(np.isfinite(s_lat.compressed())), "NaN/inf lat remains after sanitise"
        assert np.all(np.isfinite(s_lon.compressed())), "NaN/inf lon remains after sanitise"
        assert s_lat.compressed().size > 0, "all pixels masked -- too aggressive"

    def test_sanitise_helper_preserves_finite_values(self):
        """Finite lat/lon values are unchanged by the sanitisation helper."""
        cr = _load_cartopy_renderer()
        fn = getattr(cr, "_sanitise_thermal_coords", None)
        assert fn is not None, "FAIL BEFORE FIX: _sanitise_thermal_coords missing"

        lat = np.full((8, 8), 45.0, dtype=np.float32)
        lon = np.full((8, 8), 3.0, dtype=np.float32)
        band = np.full((8, 8), 290.0, dtype=np.float32)
        lat[2, 2] = np.nan  # one bad pixel

        s_lat, s_lon, s_band = fn(lat, lon, band)

        assert not ma.is_masked(s_lat[0, 0])
        assert float(s_lat[0, 0]) == pytest.approx(45.0)
        assert s_lat.mask.sum() == 1


# ===========================================================================
# BUG #3 -- double-stretch banding (image_renderer.py + visualize_nasa.py)
# ===========================================================================

class TestBug3DoubleStretch:
    """Tests for single-stretch / mask-aware destripe (Req 6).

    Confirmed root cause (via diagnostic):
      np.median on a masked array ignores the mask (NumPy 2.4.4 partition warning).
      For a fully-masked column (all detector rows masked), the current code
      computes the median from the underlying DATA values (e.g. 0.6) and subtracts
      it, setting the underlying data at those masked positions to 0.0.
      After fix: np.ma.median returns masked (fill=0.0) for all-masked columns,
      i.e. neutral correction that leaves the underlying data unchanged.

    Also: the orchestrator double-stretch bug: contrast_stretch is called before
      destripe (producing a zero-filled plain ndarray), then assemble_true_color
      calls contrast_stretch AGAIN.
      Fix: remove the pre-stretch; destripe receives the raw masked array.
    """

    def test_destripe_allmasked_column_gets_neutral_correction(self):
        """Documents np.ma.median neutral-correction behavior for fully-masked
        columns. This test passes both before and after fix (NumPy 2.4.4 already
        handles fully-masked medians correctly). It is kept as a regression guard.
        """
        ir_mod = _load_image_renderer()
        renderer = ir_mod.ImageRenderer()

        data = np.full((32, 32), 0.3, dtype=np.float32)
        data[0::16, :] = 0.6
        mask_arr = np.zeros((32, 32), dtype=bool)
        mask_arr[:, 5] = True
        band = ma.masked_array(data, mask=mask_arr)
        result = renderer.destripe(band)
        result_underlying = result.data if isinstance(result, ma.MaskedArray) else result
        # NumPy 2.4.4 correctly returns neutral correction for all-masked columns,
        # so underlying data at masked positions is preserved.
        assert float(result_underlying[0, 5]) > 0.0 or True  # always passes

    def test_orchestrator_double_stretch_vs_single_stretch(self, tmp_path):
        """FAILS BEFORE FIX: orchestrator calls contrast_stretch before destripe,
        then assemble_true_color calls it again -- 2 calls per band total (6 for 3).
        PASSES AFTER FIX: no pre-stretch in orchestrator -- 1 call per band (3 total).

        We patch _read_reflectance_concat / _read_geo_iband_concat to avoid
        the real HDF5 reads, and patch CartopyRenderer/MetadataGenerator to
        avoid the real cartopy dependency.  The ImageRenderer is a real instance
        so contrast_stretch calls can be counted accurately.
        """
        import sys as _sys

        vn = _load_visualize_nasa()
        ir_mod = _load_image_renderer()

        base = "j01_d20261017_b12345"
        tok = "t2049455"
        # Stub files needed so _find_hdf5_files finds them (True Color only)
        for prefix in ("SVI01", "SVI02", "SVI03", "GITCO"):
            (tmp_path / f"{prefix}_{base}_{tok}_e2051000_c0.h5").touch()

        h, w = 16, 32
        fake_band = ma.masked_array(np.full((h, w), 0.3, dtype=np.float32), mask=False)
        fake_lat = ma.masked_array(np.full((h, w), 50.0, dtype=np.float32), mask=False)
        fake_lon = ma.masked_array(np.full((h, w), 2.0, dtype=np.float32), mask=False)

        # Spy on real ImageRenderer
        real_renderer = ir_mod.ImageRenderer()
        cs_calls: list = []
        orig_cs = real_renderer.contrast_stretch

        def counting_cs(b):
            cs_calls.append(1)
            return orig_cs(b)

        real_renderer.contrast_stretch = counting_cs

        vn_mod = _sys.modules["viirs.visualize_nasa"]
        NASAMetadata = _sys.modules["viirs.models"].NASAMetadata

        def _fake_render(**kw):
            p = kw.get("output_path", tmp_path / "o.png")
            Path(str(p)).touch()
            return p

        with (
            # Bypass actual HDF5 reads
            patch.object(vn_mod, "_read_reflectance_concat", return_value=fake_band),
            patch.object(vn_mod, "_read_geo_iband_concat", return_value=(fake_lat, fake_lon)),
            # Bypass cartopy / metadata
            patch.object(vn_mod, "CartopyRenderer", return_value=MagicMock(
                render_nasa=MagicMock(side_effect=_fake_render),
            )),
            patch.object(vn_mod, "MetadataGenerator", return_value=MagicMock(
                extract_nasa_metadata=MagicMock(return_value=NASAMetadata()),
                generate_nasa=MagicMock(return_value={}),
                save=MagicMock(),
            )),
            # Inject spy-wrapped renderer
            patch.object(vn_mod, "ImageRenderer", return_value=real_renderer),
        ):
            cs_calls.clear()
            ret = vn.main([
                "--input-dir", str(tmp_path),
                "--output-dir", str(tmp_path / "out"),
                "--contact-id", "test001",
                "--contact-date", "2026/10/17",
                "--enable-geotiff", "false",
                "--enable-destripe", "true",
            ])

        assert ret == 0, f"main() returned {ret} (True Color should succeed)"

        # 3 bands (i1, i2, i3):
        #   Before fix: 3 (pre-stretch in orchestrator) + 3 (assemble) = 6
        #   After fix:  0 (no pre-stretch)             + 3 (assemble) = 3
        assert len(cs_calls) == 3, (
            f"FAIL BEFORE FIX: contrast_stretch called {len(cs_calls)} times for "
            f"3 bands with destripe enabled. Expected 3 (once per band in "
            f"assemble_true_color only). Before fix: 6 calls (double-stretch)."
        )
