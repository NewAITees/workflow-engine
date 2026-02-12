#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from rich.table import Table

# Add project root to path to import shared modules
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from shared.console import console, print_error, print_header, print_info  # noqa: E402

DEFAULT_JSON_FIELDS = "number,title,url,state,labels,assignees,createdAt,updatedAt"
COMPLETED_STATUSES = {"approved"}
FAILED_STATUSES = {"failed", "ci-failed"}


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        print_error(f"Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


def run_gh_command(args: list[str]) -> Any:
    """Run a GitHub CLI command and return JSON output."""
    cmd = ["gh"] + args + ["--json", DEFAULT_JSON_FIELDS]
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
        name = label.get("name", "")
        if name.startswith("status:"):
            return name.replace("status:", "")
    return "unknown"


def _parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _format_elapsed(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def _infer_agent(status: str, item_type: str) -> str:
    if item_type == "PR":
        return "reviewer" if status in {"reviewing", "in-review"} else "-"
    if status in {"ready", "implementing", "testing", "changes-requested"}:
        return "worker"
    if status in {"failed", "needs-clarification", "escalated"}:
        return "planner"
    return "-"


def _fetch_comments(repo_name: str, item_type: str, item_number: int) -> list[dict[str, Any]]:
    cmd = [
        "gh",
        "issue" if item_type == "Issue" else "pr",
        "view",
        str(item_number),
        "--repo",
        repo_name,
        "--comments",
        "--json",
        "comments",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout)
        comments = payload.get("comments", [])
        return comments if isinstance(comments, list) else []
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []


def extract_events_from_comments(
    comments: list[dict[str, Any]], item_number: int, item_type: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body", ""))
        timestamp = comment.get("created_at") or comment.get("createdAt") or ""
        if body.startswith("ACK:"):
            event_type = "ack"
        elif "WORKER_RETRY:" in body:
            event_type = "worker_retry"
        elif "PLANNER_RETRY:" in body:
            event_type = "planner_retry"
        elif re.search(r"ESCALATION:(worker|reviewer)", body, re.IGNORECASE):
            event_type = "escalation"
        elif "recovered stale lock" in body.lower():
            event_type = "stale_recovery"
        elif "processing failed" in body.lower() or "status:failed" in body.lower():
            event_type = "failed"
        else:
            continue

        events.append(
            {
                "item_type": item_type.lower(),
                "item_number": item_number,
                "event_type": event_type,
                "timestamp": timestamp,
                "body": body,
            }
        )
    return sorted(events, key=lambda event: event["timestamp"])


def compute_monitor_metrics(processed_items: list[dict[str, Any]]) -> dict[str, Any]:
    completed_count = 0
    failed_count = 0
    durations: list[int] = []
    retry_distribution = {"0": 0, "1": 0, "2": 0, "3+": 0}

    for item in processed_items:
        outcome = item.get("outcome")
        if outcome == "completed":
            completed_count += 1
        elif outcome == "failed":
            failed_count += 1

        started = _parse_timestamp(str(item.get("started_at", "")))
        finished = _parse_timestamp(str(item.get("finished_at", "")))
        if started and finished:
            delta = int((finished - started).total_seconds())
            if delta >= 0:
                durations.append(delta)

        retries = int(item.get("retry_count", 0))
        if retries <= 0:
            retry_distribution["0"] += 1
        elif retries == 1:
            retry_distribution["1"] += 1
        elif retries == 2:
            retry_distribution["2"] += 1
        else:
            retry_distribution["3+"] += 1

    total = completed_count + failed_count
    success_rate = (completed_count / total) if total else 0.0
    avg_processing_seconds = int(sum(durations) / len(durations)) if durations else 0

    return {
        "success_rate": round(success_rate, 4),
        "avg_processing_seconds": avg_processing_seconds,
        "retry_distribution": retry_distribution,
    }


def detect_duplicate_runs(
    events: list[dict[str, Any]],
    duplicate_window_seconds: int = 600,
    min_repetitions: int = 3,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[dt.datetime]] = defaultdict(list)
    for event in events:
        if event.get("event_type") != "ack":
            continue
        timestamp = _parse_timestamp(str(event.get("timestamp", "")))
        if not timestamp:
            continue
        key = (str(event.get("item_type", "")), int(event.get("item_number", 0)))
        grouped[key].append(timestamp)

    suspects: list[dict[str, Any]] = []
    for (item_type, item_number), timestamps in grouped.items():
        timestamps.sort()
        best_count = 0
        best_window: tuple[dt.datetime, dt.datetime] | None = None
        left = 0
        for right, current in enumerate(timestamps):
            while (
                current - timestamps[left]
            ).total_seconds() > duplicate_window_seconds:
                left += 1
            count = right - left + 1
            if count > best_count:
                best_count = count
                best_window = (timestamps[left], current)

        if best_count >= min_repetitions and best_window:
            suspects.append(
                {
                    "item_type": item_type,
                    "item_number": item_number,
                    "repetition_count": best_count,
                    "window_start": best_window[0].isoformat().replace("+00:00", "Z"),
                    "window_end": best_window[1].isoformat().replace("+00:00", "Z"),
                }
            )

    suspects.sort(key=lambda item: item["repetition_count"], reverse=True)
    return suspects


def build_event_timeline(
    events: list[dict[str, Any]], limit: int = 20, item_filter: str | None = None
) -> list[dict[str, Any]]:
    filtered = events
    if item_filter:
        match = re.match(r"^(issue|pr)#(\d+)$", item_filter.strip(), re.IGNORECASE)
        if match:
            target_type = match.group(1).lower()
            target_number = int(match.group(2))
            filtered = [
                event
                for event in events
                if event.get("item_type") == target_type
                and int(event.get("item_number", -1)) == target_number
            ]
        else:
            filtered = []

    ordered = sorted(filtered, key=lambda event: event.get("timestamp", ""), reverse=True)
    return ordered[:limit]


def _collect_repo_data(
    repo_name: str,
    include_comments: bool,
    max_comment_items: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    issues = run_gh_command(
        ["issue", "list", "--repo", repo_name, "--limit", "50", "--state", "open"]
    )
    prs = run_gh_command(
        ["pr", "list", "--repo", repo_name, "--limit", "50", "--state", "open"]
    )

    now = _now_utc()
    repo_data: dict[str, Any] = {"name": repo_name, "items": []}
    repo_events: list[dict[str, Any]] = []

    for item in issues + prs:
        item_type = "PR" if "/pull/" in item.get("url", "") else "Issue"
        status = get_status_from_labels(item.get("labels", []))
        updated_at = item.get("updatedAt", "")
        updated_ts = _parse_timestamp(updated_at)
        elapsed = int((now - updated_ts).total_seconds()) if updated_ts else None
        inferred_agent = _infer_agent(status, item_type)

        flags: list[str] = []
        if status in FAILED_STATUSES:
            flags.append("failed")

        repo_data["items"].append(
            {
                "number": item["number"],
                "type": item_type,
                "title": item["title"],
                "status": status,
                "url": item["url"],
                "createdAt": item.get("createdAt", ""),
                "updatedAt": updated_at,
                "elapsed_seconds": elapsed,
                "elapsed": _format_elapsed(elapsed),
                "agent": inferred_agent,
                "flags": flags,
            }
        )
        repo_events.append(
            {
                "item_type": item_type.lower(),
                "item_number": item["number"],
                "event_type": status,
                "timestamp": updated_at,
            }
        )

    if include_comments:
        for item in repo_data["items"][:max_comment_items]:
            comments = _fetch_comments(repo_name, item["type"], item["number"])
            events = extract_events_from_comments(
                comments, item_number=item["number"], item_type=item["type"]
            )
            for event in events:
                if event["event_type"] in {"stale_recovery", "escalation", "failed"}:
                    item["flags"].append(event["event_type"])
            repo_events.extend(events)

    return repo_data, repo_events


def _derive_processed_items(
    repo_items: list[dict[str, Any]], all_events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    retry_counts: dict[tuple[str, int], int] = defaultdict(int)
    start_times: dict[tuple[str, int], str] = {}
    for event in all_events:
        key = (str(event.get("item_type", "")), int(event.get("item_number", 0)))
        event_type = str(event.get("event_type", ""))
        if event_type in {"worker_retry", "planner_retry"}:
            retry_counts[key] += 1
        if event_type == "ack":
            ts = str(event.get("timestamp", ""))
            if ts and (key not in start_times or ts < start_times[key]):
                start_times[key] = ts

    processed: list[dict[str, Any]] = []
    for item in repo_items:
        status = str(item.get("status", ""))
        if status not in COMPLETED_STATUSES | FAILED_STATUSES:
            continue
        item_type = str(item.get("type", "")).lower()
        key = (item_type, int(item["number"]))
        outcome = "completed" if status in COMPLETED_STATUSES else "failed"
        processed.append(
            {
                "item_number": item["number"],
                "item_type": item_type,
                "started_at": start_times.get(key) or item.get("createdAt", ""),
                "finished_at": item.get("updatedAt", ""),
                "outcome": outcome,
                "retry_count": retry_counts.get(key, 0),
            }
        )

    return processed


def _render_dashboard(
    dashboard_data: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    metrics: dict[str, Any],
    duplicate_suspects: list[dict[str, Any]],
) -> None:
    print_header("Workflow Engine Monitor")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Repository", style="cyan")
    table.add_column("Agent", width=9)
    table.add_column("Target", style="dim")
    table.add_column("Phase", style="bold")
    table.add_column("Elapsed", style="dim")
    table.add_column("Flags")
    table.add_column("Title")

    has_items = False
    for repo in dashboard_data:
        for item in repo["items"]:
            has_items = True
            status_style = "white"
            if item["status"] == "ready":
                status_style = "green"
            elif item["status"] in {"implementing", "testing"}:
                status_style = "blue"
            elif item["status"] in {"reviewing", "in-review"}:
                status_style = "yellow"
            elif item["status"] in FAILED_STATUSES:
                status_style = "red"

            flags_text = ", ".join(sorted(set(item.get("flags", [])))) or "-"
            table.add_row(
                repo["name"],
                item.get("agent", "-"),
                f"{item['type'].lower()}#{item['number']}",
                f"[{status_style}]{item['status']}[/]",
                item.get("elapsed", "-"),
                flags_text,
                item["title"],
            )

    if has_items:
        console.print(table)
    else:
        print_info("No active items found.")

    metrics_table = Table(show_header=False)
    metrics_table.add_column("Metric", style="cyan")
    metrics_table.add_column("Value")
    metrics_table.add_row(
        "Success rate",
        f"{metrics['success_rate'] * 100:.1f}%",
    )
    metrics_table.add_row(
        "Avg processing",
        _format_elapsed(metrics["avg_processing_seconds"]),
    )
    retries = metrics.get("retry_distribution", {})
    metrics_table.add_row(
        "Retries (0/1/2/3+)",
        f"{retries.get('0', 0)}/{retries.get('1', 0)}/{retries.get('2', 0)}/{retries.get('3+', 0)}",
    )
    metrics_table.add_row("Duplicate suspects", str(len(duplicate_suspects)))
    console.print(metrics_table)

    timeline_table = Table(show_header=True, header_style="bold magenta")
    timeline_table.add_column("Time", style="dim")
    timeline_table.add_column("Item")
    timeline_table.add_column("Event")
    for event in timeline:
        timeline_table.add_row(
            str(event.get("timestamp", ""))[:19].replace("T", " "),
            f"{event.get('item_type', '?')}#{event.get('item_number', '?')}",
            str(event.get("event_type", "")),
        )
    if timeline:
        console.print(timeline_table)
    else:
        print_info("No timeline events in current scope.")


def _build_dashboard_payload(
    repositories: list[dict[str, Any]],
    include_comments: bool,
    max_comment_items: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    dashboard_data: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []

    for repo in repositories:
        repo_name = repo["name"]
        repo_data, repo_events = _collect_repo_data(
            repo_name=repo_name,
            include_comments=include_comments,
            max_comment_items=max_comment_items,
        )
        dashboard_data.append(repo_data)
        all_events.extend(repo_events)
        all_items.extend(repo_data["items"])

    processed_items = _derive_processed_items(all_items, all_events)
    metrics = compute_monitor_metrics(processed_items)
    duplicate_suspects = detect_duplicate_runs(all_events)
    return dashboard_data, all_events, metrics, duplicate_suspects


def main() -> None:
    parser = argparse.ArgumentParser(description="Workflow Engine Status Dashboard")
    parser.add_argument("repo_filter", nargs="?", help="Filter by repository name (owner/repo)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--watch", action="store_true", help="Continuously refresh dashboard")
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Refresh interval seconds (watch mode only)",
    )
    parser.add_argument(
        "--timeline-limit",
        type=int,
        default=20,
        help="Number of recent events to show in timeline",
    )
    parser.add_argument(
        "--item",
        dest="item_filter",
        help="Filter timeline by item (example: issue#29 or pr#41)",
    )
    parser.add_argument(
        "--max-comment-items",
        type=int,
        default=10,
        help="Max open items per repo to inspect comments for event parsing",
    )
    args = parser.parse_args()

    config_path = project_root / "config" / "repos.yml"
    config = load_config(config_path)

    repositories = config.get("repositories", [])
    if args.repo_filter:
        repositories = [r for r in repositories if args.repo_filter in r["name"]]

    if not repositories:
        print_info("No repositories found to check.")
        return

    while True:
        include_comments = not args.json
        dashboard_data, all_events, metrics, duplicate_suspects = _build_dashboard_payload(
            repositories=repositories,
            include_comments=include_comments,
            max_comment_items=max(0, args.max_comment_items),
        )

        if args.json:
            print(json.dumps(dashboard_data, indent=2, ensure_ascii=False))
        else:
            timeline = build_event_timeline(
                all_events,
                limit=max(1, args.timeline_limit),
                item_filter=args.item_filter,
            )
            if args.watch:
                console.clear()
            _render_dashboard(
                dashboard_data=dashboard_data,
                timeline=timeline,
                metrics=metrics,
                duplicate_suspects=duplicate_suspects,
            )

        if not args.watch:
            return
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
