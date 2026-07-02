"""CSPP SDR Processing Script for NOAA-20 RDR → SDR + GEO calibration.

Wraps the CSPP (Community Satellite Processing Package) SDR processor to
convert RDR (HDF5 Level 0) files produced by RT-STPS into calibrated
SDR and GEO (HDF5 Level 1) files via the viirs_sdr.sh script.

Requirements satisfied:
  4.1 — Execute CSPP SDR to produce SDR and GEO files from RDR input
  4.2 — Produce SDR files for all VIIRS bands (I1-I5, M1-M16, DNB)
  4.3 — Produce GEO companion files (GIGTO, GMODO, GDNBO)
  4.5 — On per-granule failure, continue and record; success if ≥ 1 granule succeeded
  4.6 — Raise TotalCSPPFailure if zero SDR files are produced

Usage:
    python cspp_process.py <rdr_dir> <output_dir>

Exit codes:
    0  success or partial success (at least one granule produced SDR output)
    1  total failure (zero SDR files produced)
"""

import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TotalCSPPFailure(RuntimeError):
    """Raised when CSPP SDR produces zero SDR files across all granules."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class CSPPResult:
    """Result of a CSPP SDR processing run."""

    sdr_files: list[str] = field(default_factory=list)
    geo_files: list[str] = field(default_factory=list)
    granules_processed: int = 0
    granules_failed: int = 0
    failed_granules: list[dict] = field(default_factory=list)
    bands_produced: set[str] = field(default_factory=set)
    status: str = "failure"  # "success" | "partial" | "failure"


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------


class CSPPProcessor:
    """Wrapper around CSPP SDR for RDR → SDR + GEO calibration."""

    CSPP_HOME = os.environ.get("CSPP_HOME", "/opt/cspp-sdr")

    EXPECTED_SDR_PATTERNS = {
        "I-band": "SVI0{1,2,3,4,5}_npp_d*_t*_*.h5",
        "M-band": "SVM{01-16}_npp_d*_t*_*.h5",
        "DNB": "SVDNB_npp_d*_t*_*.h5",
    }
    EXPECTED_GEO_PATTERNS = {
        "I-band GEO": "GIGTO_npp_d*_t*_*.h5",
        "M-band GEO": "GMODO_npp_d*_t*_*.h5",
        "DNB GEO": "GDNBO_npp_d*_t*_*.h5",
    }

    def process(self, rdr_dir: str, output_dir: str) -> CSPPResult:
        """Execute CSPP SDR on all RDR files in rdr_dir.

        Processes each granule independently — a per-granule failure is
        tolerated as long as at least one granule succeeds.

        Args:
            rdr_dir:    Directory containing RDR .h5 input files.
            output_dir: Directory where SDR and GEO files are written.

        Returns:
            CSPPResult with SDR/GEO file lists, granule counts, failure
            details, band coverage, and overall status.

        Raises:
            TotalCSPPFailure: If zero SDR files are produced across all
                              granules (requirement 4.6).
        """
        rdr_files = sorted(Path(rdr_dir).glob("*.h5"))
        logger.info(
            "Found %d RDR file(s) in %s", len(rdr_files), rdr_dir
        )

        granules_processed = 0
        failed_granules: list[dict] = []

        for rdr_file in rdr_files:
            granule_rdr = str(rdr_file)
            try:
                result = self._process_granule(granule_rdr, output_dir)
            except Exception as exc:  # noqa: BLE001
                result = {
                    "status": "failure",
                    "granule": granule_rdr,
                    "exit_code": -1,
                    "error": str(exc),
                }

            granules_processed += 1
            if result["status"] == "failure":
                logger.warning(
                    "Granule failed (exit %s): %s — %s",
                    result.get("exit_code"),
                    granule_rdr,
                    result.get("error", ""),
                )
                failed_granules.append(result)
            else:
                logger.info("Granule succeeded: %s", granule_rdr)

        granules_failed = len(failed_granules)
        sdr_files, geo_files = self._collect_outputs(output_dir)

        bands_produced: set[str] = set()
        for path in sdr_files:
            name = Path(path).name
            if name.startswith("SVI"):
                bands_produced.add("I-band")
            elif name.startswith("SVM"):
                bands_produced.add("M-band")
            elif name.startswith("SVDNB"):
                bands_produced.add("DNB")

        if not sdr_files:
            status = "failure"
        elif granules_failed == 0:
            status = "success"
        else:
            status = "partial"

        logger.info(
            "CSPP complete — status=%s, sdr=%d, geo=%d, processed=%d, failed=%d",
            status,
            len(sdr_files),
            len(geo_files),
            granules_processed,
            granules_failed,
        )

        cspp_result = CSPPResult(
            sdr_files=sdr_files,
            geo_files=geo_files,
            granules_processed=granules_processed,
            granules_failed=granules_failed,
            failed_granules=failed_granules,
            bands_produced=bands_produced,
            status=status,
        )

        if status == "failure":
            raise TotalCSPPFailure(
                f"CSPP SDR produced zero SDR files from {granules_processed} granule(s) "
                f"in rdr_dir={rdr_dir}"
            )

        return cspp_result

    def _invoke_cspp(self, rdr_files: list[str], output_dir: str) -> int:
        """Invoke CSPP SDR viirs_sdr.sh for the given RDR file(s).

        Runs in a temporary CSPP workspace directory (CSPP_WORKDIR) so that
        multiple granule invocations do not collide.

        Args:
            rdr_files:  List of RDR file paths to pass as arguments.
            output_dir: Directory where SDR/GEO output is written.

        Returns:
            Process exit code (0 = success).
        """
        viirs_sdr_sh = Path(self.CSPP_HOME) / "bin" / "viirs_sdr.sh"
        cmd = [str(viirs_sdr_sh), *rdr_files]

        with tempfile.TemporaryDirectory(prefix="cspp_workdir_") as workdir:
            env = os.environ.copy()
            env["CSPP_WORKDIR"] = workdir

            logger.info("Invoking CSPP SDR: %s", " ".join(cmd))
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=output_dir,
                env=env,
            )

        if completed.stdout:
            logger.debug("CSPP stdout:\n%s", completed.stdout)
        if completed.stderr:
            logger.debug("CSPP stderr:\n%s", completed.stderr)

        if completed.returncode != 0:
            logger.error(
                "CSPP SDR failed (exit %d).\nstdout:\n%s\nstderr:\n%s",
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )

        return completed.returncode

    def _process_granule(self, granule_rdr: str, output_dir: str) -> dict:
        """Process a single RDR granule through CSPP SDR.

        Args:
            granule_rdr: Absolute path to the single-granule RDR .h5 file.
            output_dir:  Directory where SDR/GEO output is written.

        Returns:
            dict with keys ``status`` ("success"|"failure"), ``granule``,
            ``exit_code``, and (on failure) ``error``.
        """
        try:
            exit_code = self._invoke_cspp([granule_rdr], output_dir)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failure",
                "granule": granule_rdr,
                "exit_code": -1,
                "error": str(exc),
            }

        if exit_code == 0:
            return {"status": "success", "granule": granule_rdr, "exit_code": 0}

        # Retrieve stderr from a fresh run is not possible here (already consumed
        # inside _invoke_cspp), so surface the exit code as the error detail.
        return {
            "status": "failure",
            "granule": granule_rdr,
            "exit_code": exit_code,
            "error": f"viirs_sdr.sh exited with code {exit_code}",
        }

    def _collect_outputs(self, output_dir: str) -> tuple[list[str], list[str]]:
        """Scan output_dir for SDR and GEO HDF5 files.

        SDR glob patterns:
            SVI0?_npp_d*_t*_*.h5   (I-band, 375 m)
            SVM??_npp_d*_t*_*.h5   (M-band, 750 m)
            SVDNB_npp_d*_t*_*.h5   (DNB, 750 m)

        GEO glob patterns:
            GIGTO_npp_d*_t*_*.h5   (I-band GEO)
            GMODO_npp_d*_t*_*.h5   (M-band GEO)
            GDNBO_npp_d*_t*_*.h5   (DNB GEO)

        Args:
            output_dir: Directory to scan (non-recursive).

        Returns:
            Tuple of (sdr_files, geo_files) — each a sorted list of absolute
            path strings.
        """
        base = Path(output_dir)

        sdr_patterns = [
            "SVI0?_npp_d*_t*_*.h5",
            "SVM??_npp_d*_t*_*.h5",
            "SVDNB_npp_d*_t*_*.h5",
        ]
        geo_patterns = [
            "GIGTO_npp_d*_t*_*.h5",
            "GMODO_npp_d*_t*_*.h5",
            "GDNBO_npp_d*_t*_*.h5",
        ]

        sdr_files: list[str] = []
        for pattern in sdr_patterns:
            sdr_files.extend(str(p.resolve()) for p in base.glob(pattern))

        geo_files: list[str] = []
        for pattern in geo_patterns:
            geo_files.extend(str(p.resolve()) for p in base.glob(pattern))

        logger.info(
            "Collected %d SDR file(s) and %d GEO file(s) from %s",
            len(sdr_files),
            len(geo_files),
            output_dir,
        )
        return sorted(sdr_files), sorted(geo_files)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI wrapper: cspp_process.py <rdr_dir> <output_dir>."""
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <rdr_dir> <output_dir>", file=sys.stderr)
        sys.exit(1)

    rdr_dir = sys.argv[1]
    output_dir = sys.argv[2]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    processor = CSPPProcessor()
    try:
        result = processor.process(rdr_dir, output_dir)
    except TotalCSPPFailure as exc:
        logger.error("CSPP total failure: %s", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error: %s", exc)
        sys.exit(1)

    print(f"CSPP SDR completed — status: {result.status}")
    print(f"  SDR files         : {len(result.sdr_files)}")
    print(f"  GEO files         : {len(result.geo_files)}")
    print(f"  Granules processed: {result.granules_processed}")
    print(f"  Granules failed   : {result.granules_failed}")
    print(f"  Bands produced    : {', '.join(sorted(result.bands_produced)) or 'none'}")
    if result.failed_granules:
        print("  Failed granules:")
        for fg in result.failed_granules:
            print(f"    - {fg['granule']} (exit {fg.get('exit_code')}: {fg.get('error', '')})")


if __name__ == "__main__":
    main()
