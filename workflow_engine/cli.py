"""Workflow Engine CLI entrypoint."""

from __future__ import annotations

import argparse
import subprocess
import sys
from importlib import metadata
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _script_path(command: str) -> Path:
    return _repo_root() / f"{command}-agent" / "main.py"


def _run_agent(command: str, args: list[str]) -> int:
    script = _script_path(command)
    if not script.exists():
        print(
            f"Agent script not found: {script}\n"
            "Install from the repository source or ensure the scripts are packaged.",
            file=sys.stderr,
        )
        return 2

    cmd = [sys.executable, str(script), *args]
    return subprocess.call(cmd)


def _version() -> str:
    try:
        return metadata.version("workflow-engine")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def _find_local_repo_root(start: Path) -> Path | None:
    """Find nearest local workflow-engine repo root from a starting path."""
    current = start.resolve()
    while True:
        pyproject = current / "pyproject.toml"
        if pyproject.is_file():
            if tomllib is None:
                return current
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return None
            name = data.get("project", {}).get("name")
            if name == "workflow-engine":
                return current
            return None
        if current.parent == current:
            return None
        current = current.parent


def _warn_if_execution_source_mismatch(command: str, script: Path) -> None:
    """
    Warn when running an installed copy while a local repository exists in cwd.
    """
    local_root = _find_local_repo_root(Path.cwd())
    if local_root is None:
        return

    try:
        script.relative_to(local_root)
        return  # Running local repo scripts directly.
    except ValueError:
        pass

    print(
        "Warning: execution source mismatch detected.\n"
        f"- Local repository: {local_root}\n"
        f"- Active script: {script}\n"
        "Your recent local changes may not be reflected in this run.\n"
        "Use one of:\n"
        f"  1) uv run {command}-agent/main.py owner/repo ...\n"
        "  2) pipx install . --force  (from local repository)",
        file=sys.stderr,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="workflow-engine",
        description="Workflow Engine CLI (planner/worker/reviewer)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_version()}",
    )
    parser.add_argument(
        "command",
        choices=["planner", "worker", "reviewer"],
        help="Agent to run",
    )
    parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to the agent",
    )

    parsed = parser.parse_args()
    script = _script_path(parsed.command)
    _warn_if_execution_source_mismatch(parsed.command, script)
    return _run_agent(parsed.command, parsed.args)


if __name__ == "__main__":
    raise SystemExit(main())
