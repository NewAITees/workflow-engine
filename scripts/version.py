#!/usr/bin/env python3
"""
Print the workflow-engine version from pyproject.toml.

Usage:
    python scripts/version.py
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - project requires 3.11+
    tomllib = None  # type: ignore[assignment]


def find_repo_root(start: Path) -> Path | None:
    current = start
    while True:
        if (current / "pyproject.toml").is_file():
            return current
        if current.parent == current:
            return None
        current = current.parent


def load_version(pyproject_path: Path) -> str:
    if tomllib is None:
        raise RuntimeError("tomllib is required but not available")
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to read {pyproject_path.name}: {exc}") from exc
    version = data.get("project", {}).get("version")
    if not version:
        raise RuntimeError("project.version is missing in pyproject.toml")
    return str(version)


def main() -> None:
    root = find_repo_root(Path(__file__).resolve().parent)
    if root is None:
        print("Error: pyproject.toml not found", file=sys.stderr)
        sys.exit(1)

    try:
        version = load_version(root / "pyproject.toml")
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"workflow-engine version {version}")


if __name__ == "__main__":
    main()
