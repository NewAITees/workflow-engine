#!/usr/bin/env python3
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from shared.console import (  # noqa: E402
    console,
    print_error,
    print_header,
    print_success,
    print_warning,
)


def check_command(cmd: str) -> bool:
    """Check if a command exists in the method."""
    path = shutil.which(cmd)
    if path:
        print_success(f"Found {cmd}: {path}")
        return True
    else:
        print_error(f"Missing {cmd} command")
        return False


def check_gh_auth() -> bool:
    """Check GitHub CLI authentication status."""
    try:
        subprocess.run(["gh", "auth", "status"], check=True, capture_output=True)
        print_success("GitHub CLI is authenticated")
        return True
    except subprocess.CalledProcessError:
        print_error("GitHub CLI is NOT authenticated. Run 'gh auth login'")
        return False


def check_config(config_path: Path) -> bool:
    """Validate configuration file."""
    if not config_path.exists():
        print_error(f"Config file missing: {config_path}")
        return False

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)

        repos = config.get("repositories", [])
        if not isinstance(repos, list) or not repos:
            print_warning("No repositories configured in repos.yml")
            return True  # Not a fatal error, just warning

        print_success(f"Config loaded valid: {len(repos)} repositories configured")
        return True
    except Exception as e:
        print_error(f"Config file invalid: {e}")
        return False


def main():
    print_header("Workflow Engine Health Check")

    all_passed = True

    # 1. Check Dependencies
    console.rule("[bold]Dependencies[/bold]")
    deps = ["gh", "uv", "git"]
    for dep in deps:
        if not check_command(dep):
            all_passed = False

    # 2. Check Auth
    console.rule("[bold]Authentication[/bold]")
    if not check_gh_auth():
        all_passed = False

    # 3. Check Configuration
    console.rule("[bold]Configuration[/bold]")
    config_path = project_root / "config" / "repos.yml"
    if not check_config(config_path):
        all_passed = False

    # summary
    console.rule("[bold]Summary[/bold]")
    if all_passed:
        console.print("[bold green]All systems go! 🚀[/bold green]")
    else:
        console.print(
            "[bold red]Some checks failed. Please fix the issues above.[/bold red]"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
