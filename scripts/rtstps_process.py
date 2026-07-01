"""RT-STPS Processing Script for NOAA-20 CADU → RDR conversion.

Wraps NASA RT-STPS (Real-Time Software Telemetry Processing System) to
convert a CADU (Channel Access Data Unit) file into RDR (Raw Data Record)
HDF5 files, one per instrument per temporal granule.

Instruments handled:
  - VIIRS  (critical   — absence raises NoVIIRSDataError)
  - ATMS   (non-critical — absence emits a warning)
  - CrIS   (non-critical — absence emits a warning)

Usage:
    python rtstps_process.py <cadu_path> <output_dir>

Exit codes:
    0  success
    1  RT-STPS returned a non-zero exit code (RTSTPSError)
    2  no VIIRS granule produced (NoVIIRSDataError)
    3  unexpected error
"""

import glob
import logging
import subprocess
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RTSTPSError(RuntimeError):
    """Raised when RT-STPS exits with a non-zero code."""


class NoVIIRSDataError(RuntimeError):
    """Raised when no VIIRS RDR granule is found in the output directory."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class RTSTPSResult:
    """Result of a single RT-STPS processing run."""

    rdr_files: list[str] = field(default_factory=list)
    instruments_found: set[str] = field(default_factory=set)
    instruments_missing: set[str] = field(default_factory=set)
    viirs_granules: int = 0
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------


class RTSTPSProcessor:
    """Wrapper around NASA RT-STPS for CADU → RDR conversion."""

    RTSTPS_HOME = "/opt/rt-stps"
    CRITICAL_INSTRUMENTS = {"VIIRS"}
    NON_CRITICAL_INSTRUMENTS = {"ATMS", "CrIS"}

    # RDR filename patterns used to identify instruments in .h5 output files.
    # Each value is a tuple of substrings; a file matches if any substring is
    # found (case-sensitive) in its basename.
    _INSTRUMENT_PATTERNS: dict[str, tuple[str, ...]] = {
        "VIIRS": ("VIIRS", "RNSCA-RVIRS"),
        "ATMS": ("ATMS", "RATMS"),
        "CrIS": ("CrIS", "RCRIS"),
    }

    def process(self, cadu_path: str, output_dir: str) -> RTSTPSResult:
        """Execute RT-STPS on *cadu_path* and return an RTSTPSResult.

        Args:
            cadu_path:  Path to the input .cadu file.
            output_dir: Directory where RT-STPS should write RDR files.

        Returns:
            RTSTPSResult with discovered files, instrument sets, VIIRS granule
            count, and any advisory warnings.

        Raises:
            RTSTPSError:      If RT-STPS exits with a non-zero code.
            NoVIIRSDataError: If no VIIRS granule is found in *output_dir*.
        """
        exit_code = self._invoke_rtstps(cadu_path, output_dir)
        if exit_code != 0:
            raise RTSTPSError(
                f"RT-STPS exited with code {exit_code} for input: {cadu_path}"
            )

        # RT-STPS writes to ../data relative to output_dir (cwd)
        rdr_output_dir = str(Path(output_dir).parent / "data")
        validation = self._validate_output(rdr_output_dir)

        result = RTSTPSResult(
            rdr_files=validation["rdr_files"],
            instruments_found=validation["instruments_found"],
            instruments_missing=validation["instruments_missing"],
            viirs_granules=validation["viirs_granules"],
            warnings=validation["warnings"],
        )

        if result.viirs_granules == 0:
            raise NoVIIRSDataError(
                f"No VIIRS granule produced in output directory: {output_dir}"
            )

        return result

    def _invoke_rtstps(self, cadu_path: str, output_dir: str) -> int:
        """Invoke RT-STPS via subprocess and return its exit code.

        The RT-STPS batch script is called with the bundled XML config and the
        input .cadu file.  stdout/stderr are captured and logged so callers can
        diagnose failures without losing RT-STPS output.

        Args:
            cadu_path:  Path to the input .cadu file.
            output_dir: Directory written to by RT-STPS.

        Returns:
            Process exit code (0 = success).
        """
        batch_sh = Path(self.RTSTPS_HOME) / "bin" / "batch.sh"
        config_xml = Path(self.RTSTPS_HOME) / "config" / "npp.xml"

        # RT-STPS npp.xml writes RDR files to a relative path "../data" from
        # its working directory.  Ensure that directory exists.
        data_dir = Path(output_dir).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Ensured RT-STPS output directory exists: %s", data_dir)

        cmd = [str(batch_sh), str(config_xml), str(cadu_path)]
        logger.info("Invoking RT-STPS: %s", " ".join(cmd))

        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=output_dir,
        )

        if completed.stdout:
            logger.debug("RT-STPS stdout:\n%s", completed.stdout)
        if completed.stderr:
            logger.debug("RT-STPS stderr:\n%s", completed.stderr)

        if completed.returncode != 0:
            logger.error(
                "RT-STPS failed (exit %d).\nstdout:\n%s\nstderr:\n%s",
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )

        return completed.returncode

    def _validate_output(self, output_dir: str) -> dict:
        """Scan *output_dir* for RDR HDF5 files and validate instrument coverage.

        Emits a Python warning (via the ``warnings`` module) for each expected
        non-critical instrument that is absent from the output.

        Args:
            output_dir: Directory to scan for .h5 RDR files.

        Returns:
            dict with keys:
                rdr_files         – list[str] of absolute paths to all .h5 files
                instruments_found – set[str] of instruments with ≥ 1 granule
                instruments_missing – set[str] of expected instruments with 0 granules
                viirs_granules    – int, count of VIIRS granule files
                warnings          – list[str] of advisory warning messages
        """
        rdr_files = self._find_rdr_files(output_dir)
        logger.info("Found %d RDR file(s) in %s", len(rdr_files), output_dir)

        instruments_found: set[str] = set()
        viirs_granules: int = 0

        for rdr_path in rdr_files:
            basename = Path(rdr_path).name
            for instrument, patterns in self._INSTRUMENT_PATTERNS.items():
                if any(pat in basename for pat in patterns):
                    instruments_found.add(instrument)
                    if instrument == "VIIRS":
                        viirs_granules += 1

        all_expected = self.CRITICAL_INSTRUMENTS | self.NON_CRITICAL_INSTRUMENTS
        instruments_missing = all_expected - instruments_found

        advisory_warnings: list[str] = []
        for instrument in sorted(instruments_missing & self.NON_CRITICAL_INSTRUMENTS):
            msg = f"Non-critical instrument {instrument} not found in RT-STPS output: {output_dir}"
            logger.warning(msg)
            warnings.warn(msg, UserWarning, stacklevel=3)
            advisory_warnings.append(msg)

        return {
            "rdr_files": rdr_files,
            "instruments_found": instruments_found,
            "instruments_missing": instruments_missing,
            "viirs_granules": viirs_granules,
            "warnings": advisory_warnings,
        }

    def _find_rdr_files(self, output_dir: str) -> list[str]:
        """Return a sorted list of all .h5 files in *output_dir*.

        Uses glob patterns consistent with RDR naming conventions produced by
        RT-STPS (e.g. ``RNSCA-RVIRS_npp_d*.h5``, ``RATMS_npp_d*.h5``).

        Args:
            output_dir: Directory to search (non-recursive).

        Returns:
            Sorted list of absolute path strings.
        """
        pattern = str(Path(output_dir) / "*.h5")
        matches = glob.glob(pattern)
        return sorted(matches)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI wrapper: rtstps_process.py <cadu_path> <output_dir>."""
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <cadu_path> <output_dir>", file=sys.stderr)
        sys.exit(1)

    cadu_path = sys.argv[1]
    output_dir = sys.argv[2]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    processor = RTSTPSProcessor()
    try:
        result = processor.process(cadu_path, output_dir)
    except RTSTPSError as exc:
        logger.error("RT-STPS processing failed: %s", exc)
        sys.exit(1)
    except NoVIIRSDataError as exc:
        logger.error("No VIIRS data: %s", exc)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error: %s", exc)
        sys.exit(3)

    print(f"RT-STPS completed successfully.")
    print(f"  RDR files      : {len(result.rdr_files)}")
    print(f"  VIIRS granules : {result.viirs_granules}")
    print(f"  Instruments    : {', '.join(sorted(result.instruments_found))}")
    if result.instruments_missing:
        print(f"  Missing        : {', '.join(sorted(result.instruments_missing))}")
    if result.warnings:
        print("  Warnings:")
        for w in result.warnings:
            print(f"    - {w}")


if __name__ == "__main__":
    main()
