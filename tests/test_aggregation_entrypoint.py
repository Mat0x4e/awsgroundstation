"""Contract tests for scripts/aggregation_entrypoint.sh (shared Batch chain).

The script is run for real under bash with stub `aws`, RT-STPS and CSPP
binaries. Hard-coded absolute paths (/opt/data, /var/tmp, /tmp, tar -C /opt/)
are rewritten into a per-test sandbox in a COPY of the script; all logic under
test (gating on PN_ENCODED, exit codes) is untouched.
"""
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "aggregation_entrypoint.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _u(path: Path) -> str:
    """Path as seen by bash (WSL translation on Windows)."""
    p = str(path)
    if sys.platform != "win32":
        return p
    drive, rest = p.replace("\\", "/").split(":", 1)
    return f"/mnt/{drive.lower()}{rest}"


def _write(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").encode())
    if executable:
        path.chmod(0o755)


STUB_AWS = r"""#!/bin/bash
# records every call; emulates just enough of s3 cp / s3 sync
echo "aws $*" >> "$STUB_ROOT/aws.log"
if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then
    case "$3" in
        s3://*static_luts_j01*) cp "$STUB_ROOT/luts.tgz" "$4" ;;
    esac
elif [ "$1" = "s3" ] && [ "$2" = "sync" ]; then
    case "$3" in
        s3://*/lut-cache/*) mkdir -p "$4"; : > "$4/package" ;;
        s3://*/cadu/|s3://*/satdump/) mkdir -p "$4"; printf 'frame' > "$4/a.cadu" ;;
    esac
fi
exit 0
"""

STUB_BATCH = r"""#!/bin/bash
case "${RDR_MODE:-viirs}" in
    viirs) : > "$DATA_DIR/RNSCA-RVIRS_x.h5" ;;
    other) : > "$DATA_DIR/RNSCA-RATMS_x.h5" ;;
    none) ;;
esac
"""

STUB_CSPP = r"""#!/bin/bash
# viirs_sdr.sh --work-dir DIR -p 4 RDR
[ "${SDR_MODE:-ok}" = "ok" ] && : > "$2/SVM15_x.h5"
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path
    cspp = root / "cspp"
    rt = root / "rtstps"
    (cspp / "anc/static/shipped_luts/j01").mkdir(parents=True)
    _write(cspp / "bin/viirs_sdr.sh", STUB_CSPP, True)
    _write(rt / "bin/batch.sh", STUB_BATCH, True)
    _write(rt / "config/jpss1.xml", '<c PnEncoded="true"/>\n')
    _write(rt / "config/jpss1.pn-encoded.xml", '<c PnEncoded="true"/>\n')
    _write(root / "bin/aws", STUB_AWS, True)
    with tarfile.open(root / "luts.tgz", "w:gz"):
        pass
    (root / "opt").mkdir()
    (root / "data").mkdir()
    script = (
        SCRIPT.read_text()
        .replace("/opt/data", _u(root / "data"))
        .replace("-C /opt/", f"-C {_u(root / 'opt')}/")
        .replace("/var/tmp/aggregation", _u(root / "work"))
        .replace("/tmp/j01luts.tgz", _u(root / "j01luts.tgz"))
    )
    copy = root / "entrypoint.sh"
    _write(copy, script)
    return root, copy


def _run(sandbox, **env_over):
    root, script = sandbox
    env = {
        "PATH": f"{_u(root / 'bin')}:/usr/local/bin:/usr/bin:/bin",
        "STUB_ROOT": _u(root),
        "DATA_DIR": _u(root / "data"),
        "CSPP_HOME": _u(root / "cspp"),
        "CSPP_SDR_HOME": _u(root / "cspp"),
        "CSPP_RT_HOME": _u(root / "cspp"),
        "RTSTPS_HOME": _u(root / "rtstps"),
        "RTSTPS": "on",
        "OUTPUT_BUCKET": "out-bucket",
        "CONTACT_ID": "c1",
        "CONTACT_DATE": "2026/11/22",
        "KMS_KEY_ID": "k",
    }
    env.update(env_over)
    if sys.platform == "win32":
        # pass env through bash -c with explicit exports (Windows env is not forwarded to WSL)
        exports = " ".join(f"export {k}='{v}';" for k, v in env.items())
        cmd = [BASH, "-c", f"{exports} bash {_u(script)}"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
    else:
        proc = subprocess.run([BASH, str(script)], capture_output=True, text=True, env=env)
    return proc, (root / "aws.log").read_text() if (root / "aws.log").exists() else ""


SYNC = {"PN_ENCODED": "true", "CADU_PREFIX": "cadu", "ASSETS_BUCKET": "assets-bucket"}
ASYNC = {}


class TestSyncOutputContract:
    def test_sync_happy_path_succeeds(self, sandbox):
        proc, log = _run(sandbox, **SYNC)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "/sdr/" in log

    def test_sync_zero_rdr_fails(self, sandbox):
        proc, _ = _run(sandbox, RDR_MODE="none", **SYNC)
        assert proc.returncode != 0
        assert "no VIIRS RDR" in proc.stdout

    def test_sync_non_viirs_rdr_only_fails_but_uploads_rdr_evidence(self, sandbox):
        proc, log = _run(sandbox, RDR_MODE="other", **SYNC)
        assert proc.returncode != 0
        assert "no VIIRS RDR" in proc.stdout
        assert "/rdr/" in log, "RDR evidence must still be uploaded before failing"

    def test_sync_zero_sdr_fails(self, sandbox):
        proc, _ = _run(sandbox, SDR_MODE="none", **SYNC)
        assert proc.returncode != 0
        assert "zero SDR/GEO" in proc.stdout


class TestAsyncUnchanged:
    def test_async_zero_rdr_still_warns_and_succeeds(self, sandbox):
        proc, _ = _run(sandbox, RDR_MODE="none", **ASYNC)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "WARN" in proc.stdout

    def test_async_zero_sdr_still_succeeds(self, sandbox):
        proc, _ = _run(sandbox, SDR_MODE="none", **ASYNC)
        assert proc.returncode == 0, proc.stdout + proc.stderr


class TestAssetsBucket:
    def test_sync_without_assets_bucket_fails_with_named_error(self, sandbox):
        proc, log = _run(sandbox, PN_ENCODED="true", CADU_PREFIX="cadu")
        assert proc.returncode != 0
        assert "ASSETS_BUCKET is required" in proc.stderr + proc.stdout
        assert "s3://out-bucket/software" not in log, "must not fall back to OUTPUT_BUCKET"

    def test_async_without_assets_bucket_falls_back_to_output_bucket(self, sandbox):
        proc, log = _run(sandbox, **ASYNC)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "s3://out-bucket/software/" in log

    def test_sync_with_assets_bucket_uses_it(self, sandbox):
        proc, log = _run(sandbox, **SYNC)
        assert "s3://assets-bucket/software/" in log
