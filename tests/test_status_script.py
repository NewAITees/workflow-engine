"""Tests for scripts/status.py helper functions."""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "status_script", Path(__file__).parent.parent / "scripts" / "status.py"
)
if spec is None or spec.loader is None:
    raise ImportError("Failed to load scripts/status.py")

status_script = importlib.util.module_from_spec(spec)
sys.modules["status_script"] = status_script
spec.loader.exec_module(status_script)


def test_get_status_from_labels_picks_status_prefix() -> None:
    labels = [{"name": "enhancement"}, {"name": "status:implementing"}]
    assert status_script.get_status_from_labels(labels) == "implementing"


def test_find_latest_ack_prefers_newest_comment_time() -> None:
    comments = [
        {
            "body": "ACK:worker:worker-abc:2026-02-12T00:00:00+00:00",
            "createdAt": "2026-02-12T00:00:00Z",
        },
        {
            "body": "ACK:worker:worker-def:2026-02-12T00:10:00+00:00",
            "createdAt": "2026-02-12T00:10:00Z",
        },
    ]
    latest = status_script.find_latest_ack(comments)
    assert latest is not None
    assert latest["agent_type"] == "worker"
    assert latest["agent_id"] == "worker-def"


def test_summarize_alerts_detects_failed_escalation_and_stale() -> None:
    now = datetime(2026, 2, 12, 1, 0, tzinfo=UTC)
    comments = [{"body": "ESCALATION:worker\n\nneed clarification", "createdAt": ""}]
    alerts = status_script.summarize_alerts(
        status="failed",
        comments=comments,
        updated_at="2026-02-12T00:00:00Z",
        stale_minutes=30,
        now=now,
    )
    assert "failed" in alerts
    assert "escalation" in alerts


def test_build_agent_statuses_marks_idle_when_no_ack() -> None:
    now = datetime(2026, 2, 12, 1, 0, tzinfo=UTC)
    repos = [{"name": "owner/repo", "items": []}]
    statuses = status_script.build_agent_statuses(repos, now)
    assert len(statuses) == 3
    for status in statuses:
        assert status["target"] == "idle"
