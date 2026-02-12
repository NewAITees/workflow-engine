#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
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


def run_gh_command(args: list[str], json_fields: list[str] | None = None) -> Any:
    """Run a GitHub CLI command and return JSON output."""
    fields = json_fields or [
        "number",
        "title",
        "url",
        "state",
        "labels",
        "assignees",
        "createdAt",
        "updatedAt",
    ]
    cmd = ["gh"] + args + ["--json", ",".join(fields)]
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


def parse_iso8601(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_status_from_labels(labels: list[dict[str, Any]]) -> str:
    """Extract status from labels (looks for status:*)."""
    for label in labels:
        name = label.get("name", "")
        if name.startswith("status:"):
            return name.replace("status:", "")
    return "unknown"


def item_target(item: dict[str, Any]) -> str:
    return f"{item['type'].lower()}#{item['number']}"


def seconds_since(timestamp: str | None, now: datetime | None = None) -> float | None:
    parsed = parse_iso8601(timestamp)
    if parsed is None:
        return None
    ref = now or datetime.now(UTC)
    return max(0.0, (ref - parsed).total_seconds())


def humanize_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    value = int(seconds)
    if value < 60:
        return f"{value}s"
    minutes, sec = divmod(value, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def filter_items_by_target(
    items: list[dict[str, Any]],
    issue_number: int | None = None,
    pr_number: int | None = None,
) -> list[dict[str, Any]]:
    filtered = items
    if issue_number is not None:
        filtered = [
            item
            for item in filtered
            if item.get("type") == "Issue" and item.get("number") == issue_number
        ]
    if pr_number is not None:
        filtered = [
            item for item in filtered if item.get("type") == "PR" and item.get("number") == pr_number
        ]
    return filtered


def build_timeline(events: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    def event_time(event: dict[str, Any]) -> datetime:
        ts = (
            event.get("created_at")
            or event.get("createdAt")
            or event.get("updatedAt")
            or event.get("updated_at")
        )
        return parse_iso8601(ts) or datetime.min.replace(tzinfo=UTC)

    return sorted(events, key=event_time, reverse=True)[: max(0, limit)]


def detect_duplicate_executions(
    runs: list[dict[str, Any]], window_seconds: int = 180
) -> list[dict[str, Any]]:
    by_target: dict[str, list[datetime]] = defaultdict(list)
    for run in runs:
        target = str(run.get("target", ""))
        started = parse_iso8601(run.get("started_at"))
        if target and started is not None:
            by_target[target].append(started)

    suspects: list[dict[str, Any]] = []
    for target, timestamps in by_target.items():
        timestamps.sort()
        max_cluster = 1
        current_cluster = 1
        for previous, current in zip(timestamps, timestamps[1:]):
            if (current - previous).total_seconds() <= window_seconds:
                current_cluster += 1
                max_cluster = max(max_cluster, current_cluster)
            else:
                current_cluster = 1
        if max_cluster >= 2:
            suspects.append({"target": target, "count": max_cluster})

    return sorted(suspects, key=lambda item: (-item["count"], item["target"]))


def compute_watch_metrics(runs: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(runs)
    successes = sum(1 for run in runs if run.get("result") == "success")
    durations = [
        float(run["duration_seconds"])
        for run in runs
        if isinstance(run.get("duration_seconds"), int | float)
    ]
    retries = [
        int(run["retries"]) for run in runs if isinstance(run.get("retries"), int)
    ]
    retry_distribution = dict(Counter(retries))
    duplicates = detect_duplicate_executions(runs)

    return {
        "success_rate": (successes / total) if total else 0.0,
        "avg_processing_time_seconds": (sum(durations) / len(durations))
        if durations
        else 0.0,
        "retry_distribution": retry_distribution,
        "duplicate_suspicions_count": len(duplicates),
        "duplicate_suspicions": duplicates,
    }


def infer_agent(status: str) -> str:
    if status == "implementing":
        return "worker"
    if status in {"reviewing", "changes-requested"}:
        return "reviewer"
    if status in {"ready", "failed"}:
        return "planner"
    return "unknown"


def parse_retry_count(labels: list[dict[str, Any]]) -> int:
    for label in labels:
        name = str(label.get("name", ""))
        if name.startswith("retry:"):
            value = name.split(":", 1)[1]
            if value.isdigit():
                return int(value)
    return 0


def build_item_signals(
    item: dict[str, Any],
    stale_seconds: int,
    now: datetime,
) -> list[str]:
    signals: list[str] = []
    status = item["status"]

    if status == "failed":
        signals.append("FAILED")

    age = seconds_since(item.get("updatedAt"), now=now)
    if status in {"implementing", "reviewing"} and age is not None and age > stale_seconds:
        signals.append("STALE")

    labels = [str(label.get("name", "")).lower() for label in item.get("labels", [])]
    if any("escalation" in label for label in labels) or "ESCALATION:" in item["title"]:
        signals.append("ESCALATION")

    return signals


def fetch_repo_items(repo_name: str, limit: int) -> list[dict[str, Any]]:
    issues = run_gh_command(
        ["issue", "list", "--repo", repo_name, "--limit", str(limit), "--state", "open"]
    )
    prs = run_gh_command(
        ["pr", "list", "--repo", repo_name, "--limit", str(limit), "--state", "open"]
    )

    items: list[dict[str, Any]] = []
    for source in [issues, prs]:
        for item in source:
            item_type = "PR" if "/pull/" in item.get("url", "") else "Issue"
            items.append(
                {
                    "number": item["number"],
                    "type": item_type,
                    "title": item["title"],
                    "status": get_status_from_labels(item.get("labels", [])),
                    "url": item["url"],
                    "updatedAt": item.get("updatedAt", ""),
                    "createdAt": item.get("createdAt", ""),
                    "labels": item.get("labels", []),
                    "assignees": item.get("assignees", []),
                }
            )

    def item_updated(entry: dict[str, Any]) -> datetime:
        return parse_iso8601(entry.get("updatedAt")) or datetime.min.replace(tzinfo=UTC)

    return sorted(items, key=item_updated, reverse=True)


def build_runs(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for item in items:
        created = parse_iso8601(item.get("createdAt"))
        updated = parse_iso8601(item.get("updatedAt"))
        duration_seconds = 0.0
        if created is not None and updated is not None:
            duration_seconds = max(0.0, (updated - created).total_seconds())
        elif updated is not None:
            duration_seconds = seconds_since(item.get("updatedAt"), now=now) or 0.0

        status = item.get("status", "")
        result = "failed" if status == "failed" else "success"

        runs.append(
            {
                "target": item_target(item),
                "result": result,
                "duration_seconds": duration_seconds,
                "retries": parse_retry_count(item.get("labels", [])),
                "started_at": item.get("createdAt") or item.get("updatedAt"),
            }
        )
    return runs


def render_dashboard(
    dashboard_data: list[dict[str, Any]],
    timeline_limit: int,
    stale_minutes: int,
) -> None:
    now = datetime.now(UTC)
    stale_seconds = stale_minutes * 60

    print_header("Workflow Engine Status Dashboard")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Repository", style="cyan")
    table.add_column("Target", style="dim")
    table.add_column("Agent", width=8)
    table.add_column("Phase", style="bold")
    table.add_column("Elapsed", style="dim", width=9)
    table.add_column("Signals", style="bold", width=20)
    table.add_column("Title")

    all_items: list[dict[str, Any]] = []
    for repo in dashboard_data:
        for item in repo["items"]:
            status_style = "white"
            if item["status"] == "ready":
                status_style = "green"
            elif item["status"] == "implementing":
                status_style = "blue"
            elif item["status"] == "reviewing":
                status_style = "yellow"
            elif item["status"] == "failed":
                status_style = "red"

            age = seconds_since(item.get("updatedAt"), now=now)
            signals = build_item_signals(item, stale_seconds=stale_seconds, now=now)
            styled_signals = ", ".join(
                f"[red]{signal}[/red]" if signal in {"FAILED", "ESCALATION"} else f"[yellow]{signal}[/yellow]"
                for signal in signals
            )

            table.add_row(
                repo["name"],
                item_target(item),
                infer_agent(item["status"]),
                f"[{status_style}]{item['status']}[/]",
                humanize_duration(age),
                styled_signals or "-",
                item["title"],
            )
            merged_item = dict(item)
            merged_item["repository"] = repo["name"]
            all_items.append(merged_item)

    if all_items:
        console.print(table)
    else:
        print_info("No active items found.")

    runs = build_runs(all_items, now=now)
    metrics = compute_watch_metrics(runs)

    metrics_table = Table(show_header=False)
    metrics_table.add_column("Metric", style="cyan")
    metrics_table.add_column("Value", style="bold")
    metrics_table.add_row("Success rate", f"{metrics['success_rate'] * 100:.1f}%")
    metrics_table.add_row(
        "Avg processing time",
        humanize_duration(metrics["avg_processing_time_seconds"]),
    )
    metrics_table.add_row(
        "Retry distribution",
        ", ".join(
            f"{retry}:{count}"
            for retry, count in sorted(metrics["retry_distribution"].items())
        )
        or "-",
    )
    metrics_table.add_row(
        "Duplicate suspicions",
        str(metrics["duplicate_suspicions_count"]),
    )
    console.print(metrics_table)

    timeline_seed = [
        {
            "id": f"{item['repository']}:{item_target(item)}",
            "created_at": item.get("updatedAt"),
            "repo": item["repository"],
            "target": item_target(item),
            "phase": item.get("status", "unknown"),
            "title": item.get("title", ""),
        }
        for item in all_items
    ]
    timeline = build_timeline(timeline_seed, limit=timeline_limit)

    timeline_table = Table(show_header=True, header_style="bold magenta")
    timeline_table.add_column("Time", style="dim", width=16)
    timeline_table.add_column("Repository", style="cyan")
    timeline_table.add_column("Target", style="dim")
    timeline_table.add_column("Phase", style="bold")
    timeline_table.add_column("Event")
    for entry in timeline:
        timeline_table.add_row(
            str(entry.get("created_at", ""))[:16].replace("T", " "),
            entry.get("repo", "-"),
            entry.get("target", "-"),
            entry.get("phase", "-"),
            entry.get("title", ""),
        )
    console.print(timeline_table)


def collect_dashboard_data(
    repositories: list[dict[str, Any]],
    limit: int,
    issue_number: int | None,
    pr_number: int | None,
    json_output: bool,
) -> list[dict[str, Any]]:
    dashboard_data: list[dict[str, Any]] = []
    for repo in repositories:
        repo_name = repo["name"]
        if not json_output:
            print_info(f"Fetching data for {repo_name}...")

        items = fetch_repo_items(repo_name, limit=limit)
        items = filter_items_by_target(
            items, issue_number=issue_number, pr_number=pr_number
        )
        dashboard_data.append({"name": repo_name, "items": items})
    return dashboard_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Workflow Engine Status Dashboard")
    parser.add_argument(
        "repo_filter", nargs="?", help="Filter by repository name (owner/repo)"
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument(
        "--watch", action="store_true", help="Continuously refresh dashboard output"
    )
    parser.add_argument(
        "--interval", type=int, default=5, help="Watch refresh interval in seconds"
    )
    parser.add_argument(
        "--timeline-limit",
        type=int,
        default=10,
        help="Number of recent events shown in timeline",
    )
    parser.add_argument(
        "--limit", type=int, default=50, help="API result limit per issue/pr list call"
    )
    parser.add_argument("--issue", type=int, help="Show only a specific issue number")
    parser.add_argument("--pr", type=int, help="Show only a specific PR number")
    parser.add_argument(
        "--stale-minutes",
        type=int,
        default=30,
        help="Treat implementing/reviewing items older than this as stale",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        print_error("--interval must be a positive integer")
        sys.exit(1)
    if args.timeline_limit <= 0:
        print_error("--timeline-limit must be a positive integer")
        sys.exit(1)
    if args.limit <= 0:
        print_error("--limit must be a positive integer")
        sys.exit(1)

    config_path = project_root / "config" / "repos.yml"
    config = load_config(config_path)

    repositories = config.get("repositories", [])
    if args.repo_filter:
        repositories = [r for r in repositories if args.repo_filter in r["name"]]

    if not repositories:
        print_info("No repositories found to check.")
        return

    def render_once() -> None:
        dashboard_data = collect_dashboard_data(
            repositories,
            limit=args.limit,
            issue_number=args.issue,
            pr_number=args.pr,
            json_output=args.json,
        )

        if args.json:
            print(json.dumps(dashboard_data, indent=2, ensure_ascii=False))
            return

        render_dashboard(
            dashboard_data,
            timeline_limit=args.timeline_limit,
            stale_minutes=args.stale_minutes,
        )

    if not args.watch:
        render_once()
        return

    while True:
        console.clear()
        render_once()
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
