"""Pytest configuration and shared fixtures."""

import sys
from pathlib import Path

# Make reviewer-agent importable as reviewer_agent_main
_repo_root = Path(__file__).parent.parent
_reviewer_agent_dir = _repo_root / "reviewer-agent"

# Ensure repo root and reviewer-agent dir are on sys.path
for _p in (_repo_root, _reviewer_agent_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Load reviewer-agent/main.py as reviewer_agent_main
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "reviewer_agent_main",
    _reviewer_agent_dir / "main.py",
    submodule_search_locations=[],
)
if _spec and _spec.loader and "reviewer_agent_main" not in sys.modules:
    import types

    _mod = types.ModuleType("reviewer_agent_main")
    _mod.__file__ = str(
        _reviewer_agent_dir / "main.py"
    )  # fix __file__ for sys.path.insert inside module
    sys.modules["reviewer_agent_main"] = _mod
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
