"""Contract tests for scripts/sync_receiver_finish.sh upload ordering.

Runs the real script under bash with a stub `aws`; absolute /var paths are
rewritten into a per-test sandbox in a COPY of the script.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_receiver_finish.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

STUB_AWS = r"""#!/bin/bash
echo "aws $*" >> "$STUB_ROOT/aws.log"
if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then
    case "$4" in
        */raw/raw_capture.bin) [ "${FAIL_RAW:-0}" = "1" ] && exit 1 ;;
    esac
fi
exit 0
"""


def _u(path: Path) -> str:
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


def _run(root: Path, raw: str | None, cadu: str | None, fail_raw: bool):
    work = root / "work" / "c1"
    work.mkdir(parents=True)
    if raw is not None:
        (work / "raw_capture.bin").write_bytes(raw.encode())
    if cadu is not None:
        (work / "combined.cadu").write_bytes(cadu.encode())
    _write(root / "bin" / "aws", STUB_AWS, True)
    script = (
        SCRIPT.read_text()
        .replace("/var/tmp/sync_receiver", _u(root / "work"))
        .replace("/var/log/sync_receiver_finish.log", _u(root / "finish.log"))
        .replace("/var/run/sync_receiver_listen.pid", _u(root / "listen.pid"))
    )
    copy = root / "finish.sh"
    _write(copy, script)
    env = {
        "PATH": f"{_u(root / 'bin')}:/usr/local/bin:/usr/bin:/bin",
        "STUB_ROOT": _u(root),
        "FAIL_RAW": "1" if fail_raw else "0",
    }
    if sys.platform == "win32":
        exports = " ".join(f"export {k}='{v}';" for k, v in env.items())
        proc = subprocess.run(
            [BASH, "-c", f"{exports} bash {_u(copy)} bkt c1 2026/11/22"],
            capture_output=True, text=True,
        )
    else:
        proc = subprocess.run(
            [BASH, str(copy), "bkt", "c1", "2026/11/22"],
            capture_output=True, text=True, env=env,
        )
    log = (root / "aws.log").read_text() if (root / "aws.log").exists() else ""
    return proc, log


def test_failing_raw_upload_does_not_block_cadu_upload(tmp_path):
    proc, log = _run(tmp_path, raw="raw", cadu="frames", fail_raw=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "/cadu/combined.cadu" in log, "parsed capture must still be uploaded"
    assert '"level":"WARN"' in proc.stdout and "Raw capture upload failed" in proc.stdout


def test_raw_uploaded_before_cadu_on_success(tmp_path):
    proc, log = _run(tmp_path, raw="raw", cadu="frames", fail_raw=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.index("/raw/raw_capture.bin") < log.index("/cadu/combined.cadu")


def test_raw_preserved_when_parse_is_empty(tmp_path):
    proc, log = _run(tmp_path, raw="raw", cadu="", fail_raw=False)
    assert proc.returncode != 0, "empty parsed capture must still fail the handoff"
    assert "/raw/raw_capture.bin" in log, "raw evidence uploaded even when parse is empty"
    assert "/cadu/combined.cadu" not in log
