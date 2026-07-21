"""Configure sys.path so that the scripts/ package is importable from tests/."""
import sys
import os

# Add the repo root to sys.path so `scripts.iq_extract` resolves correctly
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
