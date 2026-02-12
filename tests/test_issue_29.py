"""TDD tests for Issue #29 monitor/watch dashboard behavior."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _load_status_module() -> Any:
    """Load scripts/status.py as a module for direct function-level testing."""
    module_path = Path(__file__).parent.parent / "scripts" / "status.py"
    spec = importlib.util.spec_from_file_location("status_issue_29", module_path)
    if spec is None or spec.loader is None:
        raise ImportError("Failed to load scripts/status.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules["status_issue_29"] = module
    spec.loader.exec_module(module)
    return module


def test_extract_events_from_comments_supports_required_markers() -> None:
    """It should parse ACK/retry/escalation/stale/failed markers into timeline events."""
    status = _load_status_module()

    assert hasattr(status, "extract_events_from_comments")

    comments = [
        {"body": "ACK:worker:worker-a1:1700000000", "created_at": "2026-02-12T00:00:00Z"},
        {
            "body": "🔄 **Auto-retry #1**\n\nWORKER_RETRY:1",
            "created_at": "2026-02-12T00:01:00Z",
        },
        {
            "body": "✅ Planner updated spec\n\nPLANNER_RETRY:2",
            "created_at": "2026-02-12T00:02:00Z",
        },
        {
            "body": "ESCALATION:worker\n\nReason: test failures",
            "created_at": "2026-02-12T00:03:00Z",
        },
        {
            "body": "⚠️ **Recovered stale lock**\n\n- Previous status: `status:implementing`",
            "created_at": "2026-02-12T00:04:00Z",
        },
        {
            "body": "❌ **Processing failed**\n\n```trace```",
            "created_at": "2026-02-12T00:05:00Z",
        },
    ]

    events = status.extract_events_from_comments(
        comments,
        item_number=29,
        item_type="issue",
    )

    marker_types = {event["event_type"] for event in events}
    assert marker_types == {
        "ack",
        "worker_retry",
        "planner_retry",
        "escalation",
        "stale_recovery",
        "failed",
    }
    assert all(event["item_number"] == 29 for event in events)
    assert all(event["item_type"] == "issue" for event in events)


def test_compute_monitor_metrics_includes_success_rate_and_retry_buckets() -> None:
    """It should compute success rate, avg duration, and retry count distribution."""
    status = _load_status_module()

    assert hasattr(status, "compute_monitor_metrics")

    processed_items = [
        {
            "item_number": 101,
            "item_type": "issue",
            "started_at": "2026-02-12T00:00:00Z",
            "finished_at": "2026-02-12T00:10:00Z",
            "outcome": "completed",
            "retry_count": 0,
        },
        {
            "item_number": 102,
            "item_type": "issue",
            "started_at": "2026-02-12T00:00:00Z",
            "finished_at": "2026-02-12T00:20:00Z",
            "outcome": "failed",
            "retry_count": 1,
        },
        {
            "item_number": 103,
            "item_type": "pr",
            "started_at": "2026-02-12T00:00:00Z",
            "finished_at": "2026-02-12T00:15:00Z",
            "outcome": "completed",
            "retry_count": 2,
        },
        {
            "item_number": 104,
            "item_type": "issue",
            "started_at": "2026-02-12T00:00:00Z",
            "finished_at": "2026-02-12T00:15:00Z",
            "outcome": "completed",
            "retry_count": 5,
        },
    ]

    metrics = status.compute_monitor_metrics(processed_items)

    assert metrics["success_rate"] == 0.75
    assert metrics["avg_processing_seconds"] == 900
    assert metrics["retry_distribution"] == {
        "0": 1,
        "1": 1,
        "2": 1,
        "3+": 1,
    }


def test_detect_duplicate_runs_flags_short_window_repeats() -> None:
    """It should flag same item repeatedly handled in a short time window."""
    status = _load_status_module()

    assert hasattr(status, "detect_duplicate_runs")

    events = [
        {
            "item_type": "issue",
            "item_number": 29,
            "event_type": "ack",
            "timestamp": "2026-02-12T00:00:00Z",
        },
        {
            "item_type": "issue",
            "item_number": 29,
            "event_type": "ack",
            "timestamp": "2026-02-12T00:03:00Z",
        },
        {
            "item_type": "issue",
            "item_number": 29,
            "event_type": "ack",
            "timestamp": "2026-02-12T00:07:00Z",
        },
        {
            "item_type": "issue",
            "item_number": 88,
            "event_type": "ack",
            "timestamp": "2026-02-12T00:30:00Z",
        },
    ]

    suspects = status.detect_duplicate_runs(
        events,
        duplicate_window_seconds=600,
        min_repetitions=3,
    )

    assert len(suspects) == 1
    assert suspects[0]["item_type"] == "issue"
    assert suspects[0]["item_number"] == 29
    assert suspects[0]["repetition_count"] == 3


def test_build_timeline_supports_issue_pr_filter_and_limit() -> None:
    """It should return the most recent N events and allow issue/pr scoped filtering."""
    status = _load_status_module()

    assert hasattr(status, "build_event_timeline")

    events = [
        {
            "item_type": "issue",
            "item_number": 29,
            "event_type": "ack",
            "timestamp": "2026-02-12T00:00:00Z",
        },
        {
            "item_type": "pr",
            "item_number": 41,
            "event_type": "reviewing",
            "timestamp": "2026-02-12T00:01:00Z",
        },
        {
            "item_type": "issue",
            "item_number": 29,
            "event_type": "failed",
            "timestamp": "2026-02-12T00:02:00Z",
        },
        {
            "item_type": "issue",
            "item_number": 31,
            "event_type": "ready",
            "timestamp": "2026-02-12T00:03:00Z",
        },
    ]

    issue_timeline = status.build_event_timeline(events, limit=10, item_filter="issue#29")

    assert len(issue_timeline) == 2
    assert all(row["item_type"] == "issue" for row in issue_timeline)
    assert all(row["item_number"] == 29 for row in issue_timeline)

    latest_two = status.build_event_timeline(events, limit=2)
    assert [row["timestamp"] for row in latest_two] == [
        "2026-02-12T00:03:00Z",
        "2026-02-12T00:02:00Z",
    ]


def test_main_supports_watch_mode_and_uses_interval(monkeypatch) -> None:
    """`--watch` should trigger repeated refresh with the configured interval."""
    status = _load_status_module()

    monkeypatch.setattr(
        sys,
        "argv",
        ["status.py", "owner/repo", "--watch", "--interval", "1", "--json"],
    )
    monkeypatch.setattr(
        status,
        "load_config",
        lambda _path: {"repositories": [{"name": "owner/repo"}]},
    )

    calls = {"gh": 0, "sleep": 0}

    def fake_run_gh_command(_args: list[str]) -> list[dict[str, Any]]:
        calls["gh"] += 1
        return []

    def fake_sleep(interval: int) -> None:
        calls["sleep"] += 1
        if calls["sleep"] == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(status, "run_gh_command", fake_run_gh_command)
    monkeypatch.setattr(status, "time", SimpleNamespace(sleep=fake_sleep), raising=False)

    try:
        status.main()
    except KeyboardInterrupt:
        pass

    assert calls["gh"] >= 2  # issue + pr fetch in at least one refresh cycle
    assert calls["sleep"] == 1


def test_main_non_watch_mode_remains_single_shot(monkeypatch, capsys) -> None:
    """Without `--watch`, the current one-shot status output must still work."""
    status = _load_status_module()

    monkeypatch.setattr(sys, "argv", ["status.py", "owner/repo", "--json"])
    monkeypatch.setattr(
        status,
        "load_config",
        lambda _path: {"repositories": [{"name": "owner/repo"}]},
    )

    calls = {"gh": 0, "sleep": 0}

    def fake_run_gh_command(args: list[str]) -> list[dict[str, Any]]:
        calls["gh"] += 1
        if args[:2] == ["issue", "list"]:
            return [
                {
                    "number": 29,
                    "title": "watch monitor",
                    "url": "https://github.com/owner/repo/issues/29",
                    "labels": [{"name": "status:implementing"}],
                    "updatedAt": "2026-02-12T02:00:00Z",
                }
            ]
        return []

    def fake_sleep(_interval: int) -> None:
        calls["sleep"] += 1

    monkeypatch.setattr(status, "run_gh_command", fake_run_gh_command)
    monkeypatch.setattr(status, "time", SimpleNamespace(sleep=fake_sleep), raising=False)

    status.main()

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["name"] == "owner/repo"
    assert payload[0]["items"][0]["number"] == 29
    assert calls["gh"] == 2
    assert calls["sleep"] == 0
