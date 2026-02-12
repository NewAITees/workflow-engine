#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from rich.table import Table

# Add project root to path to import shared modules
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from shared.console import console, print_error, print_header, print_info  # noqa: E402

ACK_PATTERN = re.compile(
    r"^ACK:(?P<agent_type>planner|worker|reviewer):"
    r"(?P<agent_id>[^:]+):(?P<timestamp>.+)$"
)
ACTIVE_STATUSES = {
    "implementing",
    "testing",
    "reviewing",
    "in-review",
    "changes-requested",
}


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        print_error(f"Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_elapsed(started_at: datetime | None, now: datetime) -> str:
    if started_at is None:
        return "-"
    seconds = int((now - started_at).total_seconds())
    if seconds < 0:
        seconds = 0
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}"


def run_gh_json(
    args: list[str], json_fields: str | None = None
) -> list[dict[str, Any]]:
    cmd = ["gh"] + args
    if json_fields:
        cmd += ["--json", json_fields]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout) if result.stdout else []
    except subprocess.CalledProcessError as exc:
        print_error(f"GitHub CLI command failed: {' '.join(cmd)}")
        if exc.stderr:
            print_error(exc.stderr.strip())
        return []
    except json.JSONDecodeError:
        print_error("Failed to parse GitHub CLI output")
        return []


def run_gh_api_comments(repo: str, number: int, limit: int) -> list[dict[str, str]]:
    cmd = [
        "gh",
        "api",
        f"/repos/{repo}/issues/{number}/comments?per_page={limit}",
        "--jq",
        ".[] | {body: .body, createdAt: .created_at}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError:
        return []

    comments: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            comments.append(
                {
                    "body": str(item.get("body", "")),
                    "createdAt": str(item.get("createdAt", "")),
                }
            )
        except json.JSONDecodeError:
            continue
    return comments


def get_status_from_labels(labels: list[dict[str, Any]]) -> str:
    for label in labels:
        name = str(label.get("name", ""))
        if name.startswith("status:"):
            return name.replace("status:", "", 1)
    return "unknown"


def find_latest_ack(comments: list[dict[str, str]]) -> dict[str, str] | None:
    latest: dict[str, str] | None = None
    latest_at: datetime | None = None
    for comment in comments:
        body = comment.get("body", "").strip()
        match = ACK_PATTERN.match(body)
        if match is None:
            continue
        created_at = parse_timestamp(comment.get("createdAt", ""))
        if created_at is None:
            continue
        if latest_at is None or created_at > latest_at:
            latest_at = created_at
            latest = {
                "agent_type": match.group("agent_type"),
                "agent_id": match.group("agent_id"),
                "ack_timestamp": match.group("timestamp"),
                "comment_created_at": comment.get("createdAt", ""),
            }
    return latest


def summarize_alerts(
    status: str,
    comments: list[dict[str, str]],
    updated_at: str,
    stale_minutes: int,
    now: datetime,
) -> list[str]:
    alerts: list[str] = []
    body_joined = "\n".join(comment.get("body", "") for comment in comments)
    body_upper = body_joined.upper()

    if "failed" in status:
        alerts.append("failed")
    if status == "escalated" or "ESCALATION:" in body_upper:
        alerts.append("escalation")
    if "Recovered stale lock" in body_joined:
        alerts.append("stale-recovered")

    if status in ACTIVE_STATUSES:
        updated = parse_timestamp(updated_at)
        if updated is not None:
            age_minutes = (now - updated).total_seconds() / 60
            if age_minutes >= stale_minutes:
                alerts.append("stale")

    # Keep display order stable and deduplicate.
    seen: set[str] = set()
    deduped: list[str] = []
    for alert in alerts:
        if alert not in seen:
            seen.add(alert)
            deduped.append(alert)
    return deduped


def collect_repo_status(
    repo_name: str,
    stale_minutes: int,
    comment_limit: int,
    now: datetime,
) -> dict[str, Any]:
    issues = run_gh_json(
        ["issue", "list", "--repo", repo_name, "--limit", "50", "--state", "open"],
        "number,title,url,state,labels,createdAt,updatedAt",
    )
    prs = run_gh_json(
        ["pr", "list", "--repo", repo_name, "--limit", "50", "--state", "open"],
        "number,title,url,state,labels,createdAt,updatedAt",
    )

    items: list[dict[str, Any]] = []
    for item in issues + prs:
        item_type = "PR" if "/pull/" in str(item.get("url", "")) else "Issue"
        status = get_status_from_labels(item.get("labels", []))
        comments: list[dict[str, str]] = []

        # Pull comments only for active/problem items to limit API calls.
        should_fetch_comments = (
            status in ACTIVE_STATUSES
            or "failed" in status
            or status == "escalated"
            or status == "needs-clarification"
        )
        if should_fetch_comments:
            comments = run_gh_api_comments(
                repo_name, int(item["number"]), comment_limit
            )

        latest_ack = find_latest_ack(comments) if comments else None
        alerts = summarize_alerts(
            status=status,
            comments=comments,
            updated_at=str(item.get("updatedAt", "")),
            stale_minutes=stale_minutes,
            now=now,
        )

        items.append(
            {
                "number": int(item["number"]),
                "type": item_type,
                "title": str(item["title"]),
                "status": status,
                "url": str(item["url"]),
                "createdAt": str(item.get("createdAt", "")),
                "updatedAt": str(item.get("updatedAt", "")),
                "alerts": alerts,
                "latest_ack": latest_ack,
            }
        )

    return {"name": repo_name, "items": items}


def build_agent_statuses(
    repos: list[dict[str, Any]], now: datetime
) -> list[dict[str, str]]:
    latest_by_agent: dict[str, dict[str, Any]] = {}
    for repo in repos:
        repo_name = repo["name"]
        for item in repo["items"]:
            ack = item.get("latest_ack")
            if not ack:
                continue
            agent_type = ack["agent_type"]
            started = parse_timestamp(ack.get("comment_created_at", ""))
            if started is None:
                continue
            current = latest_by_agent.get(agent_type)
            if current is None or started > current["started_at"]:
                latest_by_agent[agent_type] = {
                    "agent": agent_type,
                    "agent_id": ack["agent_id"],
                    "repo": repo_name,
                    "target": f"{item['type']} #{item['number']}",
                    "phase": item["status"],
                    "elapsed": format_elapsed(started, now),
                    "started_at": started,
                }

    statuses: list[dict[str, str]] = []
    for agent in ("planner", "worker", "reviewer"):
        if agent in latest_by_agent:
            row = latest_by_agent[agent]
            statuses.append(
                {
                    "agent": row["agent"],
                    "agent_id": row["agent_id"],
                    "repo": row["repo"],
                    "target": row["target"],
                    "phase": row["phase"],
                    "elapsed": row["elapsed"],
                }
            )
        else:
            statuses.append(
                {
                    "agent": agent,
                    "agent_id": "-",
                    "repo": "-",
                    "target": "idle",
                    "phase": "-",
                    "elapsed": "-",
                }
            )
    return statuses


def style_status(status: str) -> str:
    if status == "ready":
        return "green"
    if status in {"implementing", "testing"}:
        return "blue"
    if status in {"reviewing", "in-review", "changes-requested"}:
        return "yellow"
    if "failed" in status or status == "escalated":
        return "red"
    return "white"


def style_alert(alert: str) -> str:
    if alert in {"failed", "escalation"}:
        return "bold red"
    if alert.startswith("stale"):
        return "bold yellow"
    return "white"


def render_tables(
    repos: list[dict[str, Any]],
    agent_statuses: list[dict[str, str]],
    generated_at: datetime,
    watch_mode: bool,
    interval: int,
) -> None:
    if watch_mode:
        console.clear()
    print_header("Workflow Engine Live Monitor")
    if watch_mode:
        print_info(
            f"Watch mode: every {interval}s | Last update: {generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

    agent_table = Table(show_header=True, header_style="bold magenta")
    agent_table.add_column("Agent", style="cyan", width=10)
    agent_table.add_column("Agent ID", style="dim")
    agent_table.add_column("Repository", style="cyan")
    agent_table.add_column("Current Target")
    agent_table.add_column("Phase", style="bold")
    agent_table.add_column("Elapsed", style="dim", width=10)
    for agent in agent_statuses:
        agent_table.add_row(
            agent["agent"],
            agent["agent_id"],
            agent["repo"],
            agent["target"],
            f"[{style_status(agent['phase'])}]{agent['phase']}[/]",
            agent["elapsed"],
        )
    console.print(agent_table)

    item_table = Table(show_header=True, header_style="bold magenta")
    item_table.add_column("Repository", style="cyan")
    item_table.add_column("ID", style="dim")
    item_table.add_column("Type", width=6)
    item_table.add_column("Status", style="bold")
    item_table.add_column("Alerts")
    item_table.add_column("Title")
    item_table.add_column("Updated", style="dim")

    has_items = False
    for repo in repos:
        for item in repo["items"]:
            has_items = True
            status_style = style_status(item["status"])
            alert_text = " ".join(
                f"[{style_alert(alert)}]{alert}[/]" for alert in item["alerts"]
            )
            item_table.add_row(
                repo["name"],
                f"#{item['number']}",
                item["type"],
                f"[{status_style}]{item['status']}[/]",
                alert_text if alert_text else "-",
                item["title"],
                item["updatedAt"][:16].replace("T", " "),
            )

    if has_items:
        console.print(item_table)
    else:
        print_info("No active items found.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Workflow Engine Status Dashboard")
    parser.add_argument(
        "repo_filter", nargs="?", help="Filter by repository name (owner/repo)"
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Enable live monitor mode with periodic refresh",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Refresh interval in seconds for --watch mode (default: 10)",
    )
    parser.add_argument(
        "--comment-limit",
        type=int,
        default=20,
        help="Max comments to inspect per item (default: 20)",
    )
    parser.add_argument(
        "--stale-minutes",
        type=int,
        default=30,
        help="Stale threshold in minutes for active states (default: 30)",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        print_error("--interval must be a positive integer")
        sys.exit(2)
    if args.comment_limit <= 0:
        print_error("--comment-limit must be a positive integer")
        sys.exit(2)
    if args.stale_minutes <= 0:
        print_error("--stale-minutes must be a positive integer")
        sys.exit(2)
    if args.watch and args.json:
        print_error("--watch and --json cannot be used together")
        sys.exit(2)

    config_path = project_root / "config" / "repos.yml"
    config = load_config(config_path)
    repositories = config.get("repositories", [])
    if args.repo_filter:
        repositories = [
            repo for repo in repositories if args.repo_filter in repo["name"]
        ]

    if not repositories:
        print_info("No repositories found to check.")
        return

    try:
        while True:
            now = datetime.now(UTC)
            repo_data: list[dict[str, Any]] = []
            for repo in repositories:
                stale_minutes = int(
                    repo.get("stale_lock_timeout_minutes", args.stale_minutes)
                )
                repo_data.append(
                    collect_repo_status(
                        repo_name=repo["name"],
                        stale_minutes=stale_minutes,
                        comment_limit=args.comment_limit,
                        now=now,
                    )
                )

            agent_statuses = build_agent_statuses(repo_data, now)
            if args.json:
                print(
                    json.dumps(
                        {
                            "generatedAt": now.isoformat(),
                            "agents": agent_statuses,
                            "repositories": repo_data,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return

            render_tables(repo_data, agent_statuses, now, args.watch, args.interval)
            if not args.watch:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if args.watch:
            print_info("Stopped watch mode.")


if __name__ == "__main__":
    main()
