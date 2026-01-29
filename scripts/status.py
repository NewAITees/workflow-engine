#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from rich.table import Table

# Add project root to path to import shared modules
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from shared.console import console, print_error, print_header, print_info  # noqa: E402


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        print_error(f"Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


def run_gh_command(args: list[str]) -> Any:
    """Run a GitHub CLI command and return JSON output."""
    cmd = (
        ["gh"]
        + args
        + ["--json", "number,title,url,state,labels,assignees,createdAt,updatedAt"]
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print_error(f"GitHub CLI command failed: {' '.join(cmd)}")
        print_error(e.stderr)
        return []
    except json.JSONDecodeError:
        print_error("Failed to parse GitHub CLI output")
        return []


def get_status_from_labels(labels: list[dict[str, Any]]) -> str:
    """Extract status from labels (looks for status:*)."""
    for label in labels:
        name = label["name"]
        if name.startswith("status:"):
            return name.replace("status:", "")
    return "unknown"


def main():
    parser = argparse.ArgumentParser(description="Workflow Engine Status Dashboard")
    parser.add_argument(
        "repo_filter", nargs="?", help="Filter by repository name (owner/repo)"
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    config_path = project_root / "config" / "repos.yml"
    config = load_config(config_path)

    repositories = config.get("repositories", [])
    if args.repo_filter:
        repositories = [r for r in repositories if args.repo_filter in r["name"]]

    if not repositories:
        print_info("No repositories found to check.")
        return

    dashboard_data = []

    for repo in repositories:
        repo_name = repo["name"]
        if not args.json:
            print_info(f"Fetching data for {repo_name}...")

        # Fetch Issues
        issues = run_gh_command(
            ["issue", "list", "--repo", repo_name, "--limit", "50", "--state", "open"]
        )
        # Fetch PRs
        prs = run_gh_command(
            ["pr", "list", "--repo", repo_name, "--limit", "50", "--state", "open"]
        )

        repo_data = {"name": repo_name, "items": []}

        for item in issues + prs:
            item_type = "PR" if "pull_request" in item else "Issue"
            # gh pr list output doesn't have pull_request key usually in the same way issue list might not distinguish clearly if mixed,
            # but usually we run separate commands.
            # actually strict distinction: 'url' contains /pull/ or /issues/
            if "/pull/" in item["url"]:
                item_type = "PR"
            else:
                item_type = "Issue"

            status = get_status_from_labels(item.get("labels", []))

            repo_data["items"].append(
                {
                    "number": item["number"],
                    "type": item_type,
                    "title": item["title"],
                    "status": status,
                    "url": item["url"],
                    "updatedAt": item.get("updatedAt", ""),
                }
            )

        dashboard_data.append(repo_data)

    if args.json:
        print(json.dumps(dashboard_data, indent=2, ensure_ascii=False))
        return

    # Render Table
    print_header("Workflow Engine Status Dashboard")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Repository", style="cyan")
    table.add_column("ID", style="dim")
    table.add_column("Type", width=6)
    table.add_column("Status", style="bold")
    table.add_column("Title")
    table.add_column("Updated", style="dim")

    has_items = False
    for repo in dashboard_data:
        for item in repo["items"]:
            has_items = True
            status_style = "white"
            if item["status"] == "ready":
                status_style = "green"
            elif item["status"] == "implementing":
                status_style = "blue"
            elif item["status"] == "reviewing":
                status_style = "yellow"
            elif item["status"] == "failed":
                status_style = "red"

            table.add_row(
                repo["name"],
                f"#{item['number']}",
                item["type"],
                f"[{status_style}]{item['status']}[/]",
                item["title"],
                item["updatedAt"][:16].replace("T", " "),
            )

    if has_items:
        console.print(table)
    else:
        print_info("No active items found.")


if __name__ == "__main__":
    main()
