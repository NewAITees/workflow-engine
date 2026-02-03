"""Workflow Engine CLI entrypoint."""

from __future__ import annotations

import argparse
import subprocess
import sys
from importlib import metadata
from pathlib import Path


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
    return _run_agent(parsed.command, parsed.args)


if __name__ == "__main__":
    raise SystemExit(main())
