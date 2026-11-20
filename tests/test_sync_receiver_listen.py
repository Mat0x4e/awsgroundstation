"""Unit tests for scripts/sync_receiver_listen.py.

Covers:
  - Task 9.1: READY_MARKER written on successful socket.bind();
              RECEIVING_MARKER written on first packet arrival.
  - Task 9.2: --raw-tee writes every raw UDP payload to the tee path AND
              the normal parsed-frame bytes to the main output in the same run.
  - Python 3.9 compatibility: `from __future__ import annotations` is present
                              and no PEP 604 (X | Y) syntax is used at runtime.

Sockets are fully mocked -- no real UDP traffic is opened.  The _MockSocket
helper feeds packets from a fixed list, then sets _shutdown and raises
socket.timeout so main() exits cleanly.
"""

import ast
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import scripts.sync_receiver_listen as listener_mod


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

# Minimal valid VITA-49 Signal Data packet (type=1, 7-word header, 4-byte payload).
#   header word 0: top nibble = 1 (Signal Data), low 16 bits = 8 words (size incl. header)
#   8 words × 4 = 32 bytes total; words 1-6 are zero padding; word 7 is the payload.
_HEADER_WORDS = 7
_FRAME_PAYLOAD = b"\xDE\xAD\xBE\xEF"
_PKT_SIZE_WORDS = _HEADER_WORDS + 1  # 8 words
_VITA49_PKT = (
    ((0x1 << 28) | _PKT_SIZE_WORDS).to_bytes(4, "big")
    + b"\x00" * ((_HEADER_WORDS - 1) * 4)
    + _FRAME_PAYLOAD
)

# A packet that does NOT pass as VITA-49 Signal Data (type != 1) -- raw-tee
# must still capture it, but the parsed output must not.
_NON_SIGNAL_PKT = b"\x40\x00\x00\x04" + b"\xAA\xBB\xCC\xDD" * 3  # type=4 (Context)


class _MockSocket:
    """Feeds packets from a list, then signals shutdown and raises socket.timeout.

    The _shutdown flag is set on the listener module before raising socket.timeout
    so the listen loop exits cleanly on its next iteration check.
    """

    def __init__(self, packets):
        self._packets = list(packets)
        self._idx = 0

    def setsockopt(self, *args, **kwargs):
        pass

    def bind(self, *args, **kwargs):
        pass

    def settimeout(self, *args, **kwargs):
        pass

    def recvfrom(self, bufsize):
        if self._idx < len(self._packets):
            pkt = self._packets[self._idx]
            self._idx += 1
            return pkt, ("127.0.0.1", 12345)
        # All packets delivered -- stop the listen loop.
        listener_mod._shutdown = True
        raise socket.timeout

    def close(self):
        pass


@pytest.fixture(autouse=True)
def reset_shutdown():
    """Reset the module-level _shutdown flag before and after every test."""
    listener_mod._shutdown = False
    yield
    listener_mod._shutdown = False


def _run_main(tmp_path, extra_args=None, packets=None):
    """Run listener_mod.main() with a mocked socket and temp-dir marker paths.

    Returns (output_path, ready_marker_path, receiving_marker_path).
    """
    packets = packets or []
    output_path = tmp_path / "output.cadu"
    ready_marker = tmp_path / "ready"
    receiving_marker = tmp_path / "receiving"

    argv = ["sync_receiver_listen.py", "9000", str(output_path)]
    if extra_args:
        argv.extend(extra_args)

    mock_sock = _MockSocket(packets)

    with (
        patch.object(sys, "argv", argv),
        patch.object(listener_mod, "READY_MARKER", str(ready_marker)),
        patch.object(listener_mod, "RECEIVING_MARKER", str(receiving_marker)),
        patch("socket.socket", return_value=mock_sock),
    ):
        listener_mod.main()

    return output_path, ready_marker, receiving_marker


# ---------------------------------------------------------------------------
# Task 9.1 — Readiness markers
# ---------------------------------------------------------------------------


class TestReadyMarker:
    """READY_MARKER is written immediately after socket.bind() succeeds."""

    def test_written_on_bind_with_no_packets(self, tmp_path):
        """The ready marker must exist after main() exits, even with zero packets."""
        _, ready_marker, _ = _run_main(tmp_path, packets=[])
        assert ready_marker.exists(), "READY_MARKER not written after socket.bind()"

    def test_written_on_bind_with_packets(self, tmp_path):
        """The ready marker is still present after packets are processed."""
        _, ready_marker, _ = _run_main(tmp_path, packets=[_VITA49_PKT])
        assert ready_marker.exists(), "READY_MARKER must persist through packet processing"


class TestReceivingMarker:
    """RECEIVING_MARKER is written on first packet arrival; absent with no packets."""

    def test_written_on_first_packet(self, tmp_path):
        """RECEIVING_MARKER appears once the first packet has been processed."""
        _, _, receiving_marker = _run_main(tmp_path, packets=[_VITA49_PKT])
        assert receiving_marker.exists(), "RECEIVING_MARKER not written after first packet"

    def test_absent_with_no_packets(self, tmp_path):
        """With no packets RECEIVING_MARKER must NOT be written -- PREPASS may be
        before AOS, so 'bound but no packets yet' is the expected normal state."""
        _, _, receiving_marker = _run_main(tmp_path, packets=[])
        assert not receiving_marker.exists(), (
            "RECEIVING_MARKER must not exist when no packets arrived"
        )

    def test_written_only_once_for_multiple_packets(self, tmp_path):
        """Sending two packets must not raise -- RECEIVING_MARKER is touched once."""
        _, _, receiving_marker = _run_main(
            tmp_path, packets=[_VITA49_PKT, _VITA49_PKT]
        )
        assert receiving_marker.exists()


# ---------------------------------------------------------------------------
# Task 9.2 — --raw-tee dual output
# ---------------------------------------------------------------------------


class TestRawTee:
    """--raw-tee writes raw payloads to the tee path AND parsed frames to main output."""

    def test_tee_file_contains_raw_packet(self, tmp_path):
        """The tee file must contain the exact raw packet bytes."""
        tee_path = tmp_path / "raw.bin"
        _run_main(
            tmp_path,
            extra_args=["--raw-tee", str(tee_path)],
            packets=[_VITA49_PKT],
        )
        assert tee_path.exists(), "--raw-tee file was not created"
        assert tee_path.read_bytes() == _VITA49_PKT, (
            "Tee file content must equal the original raw packet"
        )

    def test_main_output_still_has_parsed_frame(self, tmp_path):
        """Main output must contain the VITA-49 frame payload even when --raw-tee is set.
        Removing --raw-tee support would still leave the parsed output, but removing
        the tee write would cause test_tee_file_contains_raw_packet to fail.
        This test confirms --raw-tee does NOT suppress the parsed output."""
        tee_path = tmp_path / "raw.bin"
        output_path, _, _ = _run_main(
            tmp_path,
            extra_args=["--raw-tee", str(tee_path)],
            packets=[_VITA49_PKT],
        )
        assert output_path.read_bytes() == _FRAME_PAYLOAD, (
            "--raw-tee must not suppress the parsed frame output"
        )

    def test_tee_captures_non_signal_packets_excluded_from_parsed_output(self, tmp_path):
        """Non-Signal-Data packets (e.g. Context type=4) must appear in the raw tee
        but must NOT appear in the parsed output (extract_frame_payload returns None)."""
        tee_path = tmp_path / "raw.bin"
        output_path, _, _ = _run_main(
            tmp_path,
            extra_args=["--raw-tee", str(tee_path)],
            packets=[_VITA49_PKT, _NON_SIGNAL_PKT],
        )
        # Raw tee gets both packets concatenated
        assert tee_path.read_bytes() == _VITA49_PKT + _NON_SIGNAL_PKT
        # Parsed output gets only the frame from the valid Signal Data packet
        assert output_path.read_bytes() == _FRAME_PAYLOAD

    def test_no_tee_file_without_flag(self, tmp_path):
        """Without --raw-tee no extra file is written; parsed output is unaffected."""
        unexpected_tee = tmp_path / "raw.bin"
        output_path, _, _ = _run_main(tmp_path, packets=[_VITA49_PKT])
        assert not unexpected_tee.exists(), "No tee file should be created without --raw-tee"
        assert output_path.read_bytes() == _FRAME_PAYLOAD

    def test_tee_is_empty_with_no_packets(self, tmp_path):
        """When --raw-tee is given but no packets arrive the tee file exists but is empty."""
        tee_path = tmp_path / "raw.bin"
        _run_main(tmp_path, extra_args=["--raw-tee", str(tee_path)], packets=[])
        assert tee_path.exists(), "--raw-tee file must be created even with no packets"
        assert tee_path.read_bytes() == b"", "Tee file must be empty when no packets arrived"



# ---------------------------------------------------------------------------
# Python 3.9 compatibility tests
# ---------------------------------------------------------------------------


class TestPython39Compatibility:
    """Regression tests to ensure sync_receiver_listen.py is importable on Python 3.9.
    
    PEP 604 type union syntax (X | Y) was added in Python 3.10 and requires
    `from __future__ import annotations` to work on 3.9. This test verifies the fix.
    """

    def test_from_future_annotations_present(self):
        """The module must have `from __future__ import annotations` as the first import."""
        source = Path(__file__).parent.parent / "scripts" / "sync_receiver_listen.py"
        with open(source) as f:
            lines = f.readlines()

        # Find the first non-docstring, non-shebang line
        import_found = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Skip shebang, docstring, and blank lines
            if stripped.startswith("#!") or stripped.startswith('"""') or stripped.startswith("'''") or not stripped:
                continue
            # First real statement should be the __future__ import
            if "__future__" in line and "annotations" in line:
                import_found = True
                break
            # If we hit any other import before __future__, fail
            if stripped.startswith("import ") or stripped.startswith("from "):
                break

        assert import_found, (
            "`from __future__ import annotations` must be present as the first import "
            "(after docstring) to support PEP 604 syntax on Python 3.9"
        )

    def test_ast_parses_with_python39_feature_version(self):
        """The module must parse successfully when AST is set to Python 3.9 compatibility.
        
        This catches any PEP 604 (X | Y) syntax, PEP 617 pattern matching (match/case),
        or other Python 3.10+ features that would fail on 3.9.
        """
        source = Path(__file__).parent.parent / "scripts" / "sync_receiver_listen.py"
        with open(source) as f:
            source_code = f.read()

        # This will raise SyntaxError if the code uses Python 3.10+ syntax
        # (e.g., `bytes | None` instead of `Optional[bytes]`)
        try:
            ast.parse(source_code, feature_version=(3, 9))
        except SyntaxError as e:
            pytest.fail(
                f"Module contains Python 3.10+ syntax that is not compatible with Python 3.9: {e}"
            )

    def test_no_pep604_unions_in_annotations(self):
        """Verify no bare PEP 604 union syntax (X | Y) appears outside of string annotations.
        
        With `from __future__ import annotations`, all annotations are strings at
        definition time, so they're safe. But any runtime use of `X | Y` (e.g. in
        `isinstance(x, int | str)`) would still fail on 3.9.
        """
        source = Path(__file__).parent.parent / "scripts" / "sync_receiver_listen.py"
        with open(source) as f:
            source_code = f.read()

        tree = ast.parse(source_code)

        # Walk the AST looking for BinOp nodes with BitOr operator outside of strings
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                # This is a `|` operator used at runtime (not in an annotation string)
                # which would fail on 3.9
                pytest.fail(
                    f"Found PEP 604 union syntax (X | Y) at runtime (line {node.lineno}). "
                    "This will fail on Python 3.9. Use `Union[X, Y]` or "
                    "`isinstance(x, (X, Y))` instead."
                )
