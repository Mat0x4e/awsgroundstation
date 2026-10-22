"""Guard tests -- NO_SOURCE stub buildspec safety contract.

Contract: every NO_SOURCE CodeBuild project must have a stub buildspec that exits
non-zero if it ever runs without a buildspecOverride injected at start_build() time.

Background: three real defects once hid in the gap between the stub and the real
buildspec (see docs/ARCHITECTURE.md).  A stub that silently exits 0 masks a missing
override rather than failing loudly, so the contract is: stub must contain `exit 1`.

Projects covered:
  - infra/modules/viirs_visualization/codebuild.tf  (aws_codebuild_project.viirs_visualization)

NOTE: the sdr_pipeline NO_SOURCE stub guard was removed when aws_codebuild_project.sdr_pipeline
was retired to AWS Batch in the clean-compute-pipeline spec (task 8).  The project no longer
exists -- infra/modules/sdr_pipeline/codebuild.tf is now a comment-only retirement note.

How to extract: the stub is an inline double-quoted string inside the Terraform
`source { type = "NO_SOURCE" ... buildspec = "..." }` block.  A regex extracts it
from the raw .tf text without requiring a full HCL parser.
"""
import re
import pathlib

_REPO_ROOT = pathlib.Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_tf(relative_path: str) -> str:
    """Read a .tf file relative to the repo root."""
    return (_REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _extract_no_source_stub(tf_content: str) -> str:
    """Return the raw buildspec string from the first NO_SOURCE source block.

    Handles Terraform escape sequences (\\n, \\", etc.) in the inline string.
    Returns the captured group content — not Python-unescaped — so callers can
    search for literal text like 'exit 1' directly.

    Raises AssertionError if the NO_SOURCE block or buildspec attribute is absent.
    """
    # Match the source { } block that declares type = "NO_SOURCE".
    # [^}]* stops at the first closing brace, so nested blocks cannot bleed in.
    block_match = re.search(
        r'source\s*\{[^}]*type\s*=\s*"NO_SOURCE"[^}]*\}',
        tf_content,
        re.DOTALL,
    )
    assert block_match is not None, (
        "No `source { type = \"NO_SOURCE\" }` block found in the Terraform file. "
        "The extractor regex may need updating if the block structure changed."
    )
    block = block_match.group(0)

    # Extract the buildspec inline-string value.
    # Pattern: buildspec = "..." where the value may contain \" and \n escapes.
    spec_match = re.search(r'buildspec\s*=\s*"((?:[^"\\]|\\.)*)"', block)
    assert spec_match is not None, (
        "No `buildspec = \"...\"` attribute found in the NO_SOURCE source block. "
        "The extractor regex may need updating if the attribute format changed."
    )
    return spec_match.group(1)


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------

class TestNoSourceStubSafety:
    """
    GUARD: NO_SOURCE stubs must exit non-zero when run without a buildspecOverride.

    These tests FAIL if 'exit 1' is removed from a stub — that failure is intentional.
    A failing guard test means the safety net has been broken; the .tf must be fixed.
    """

    def test_viirs_visualization_stub_exits_nonzero(self):
        """viirs_visualization stub must contain 'exit 1' to fail safe if run standalone.

        Expected stub (as of Wave 6 implementation):
            version: 0.2\\nphases:\\n  build:\\n    commands:\\n
              - echo 'Buildspec must be provided via overrideSpec at start_build() time'\\n
              - exit 1\\n
        """
        tf_content = _read_tf("infra/modules/viirs_visualization/codebuild.tf")
        stub = _extract_no_source_stub(tf_content)
        assert "exit 1" in stub, (
            "GUARD BROKEN: viirs_visualization NO_SOURCE stub must contain 'exit 1'.\n"
            "If the stub runs without a buildspecOverride it must fail loudly.\n"
            "Restore '- exit 1' to the buildspec stub in:\n"
            "  infra/modules/viirs_visualization/codebuild.tf\n"
            f"Current stub extracted:\n  {stub!r}"
        )


