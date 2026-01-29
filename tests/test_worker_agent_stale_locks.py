"""Tests for WorkerAgent stale lock recovery."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "worker-agent"))

import main as worker_main
from shared.config import AgentConfig
from shared.github_client import Issue, PullRequest


def _make_agent():
    agent = worker_main.WorkerAgent.__new__(worker_main.WorkerAgent)
    agent.repo = "owner/repo"
    agent.config = AgentConfig(repo="owner/repo", stale_lock_timeout_minutes=30)
    agent.github = MagicMock()
    return agent


def test_recover_stale_issue_lock():
    agent = _make_agent()
    fixed_now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    agent._now = lambda: fixed_now

    issue = Issue(number=1, title="t", body="", labels=[agent.STATUS_IMPLEMENTING])
    agent.github.list_issues.return_value = [issue]
    agent.github.list_prs.return_value = []
    agent.github.get_issue_comments.return_value = [
        {"created_at": "2024-01-01T10:00:00Z"}
    ]
    agent.github.get_issue_events.return_value = [
        {
            "event": "labeled",
            "created_at": "2024-01-01T09:59:00Z",
            "label": agent.STATUS_READY,
        },
        {
            "event": "labeled",
            "created_at": "2024-01-01T10:00:00Z",
            "label": agent.STATUS_IMPLEMENTING,
        },
    ]

    agent._process_stale_locks()

    agent.github.remove_label.assert_called_with(issue.number, agent.STATUS_IMPLEMENTING)
    agent.github.add_label.assert_called_with(issue.number, agent.STATUS_READY)
    agent.github.comment_issue.assert_called_once()


def test_recover_stale_pr_lock_changes_requested():
    agent = _make_agent()
    fixed_now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    agent._now = lambda: fixed_now

    pr = PullRequest(
        number=2,
        title="t",
        body="",
        labels=[agent.STATUS_IMPLEMENTING],
        head_ref="feature",
        base_ref="main",
    )
    agent.github.list_issues.return_value = []
    agent.github.list_prs.return_value = [pr]
    agent.github.get_issue_comments.return_value = [
        {"created_at": "2024-01-01T10:00:00Z"}
    ]
    agent.github.get_issue_events.return_value = [
        {
            "event": "labeled",
            "created_at": "2024-01-01T09:50:00Z",
            "label": agent.STATUS_CHANGES_REQUESTED,
        },
        {
            "event": "labeled",
            "created_at": "2024-01-01T10:00:00Z",
            "label": agent.STATUS_IMPLEMENTING,
        },
    ]

    agent._process_stale_locks()

    agent.github.remove_pr_label.assert_called_with(pr.number, agent.STATUS_IMPLEMENTING)
    agent.github.add_pr_label.assert_called_with(
        pr.number, agent.STATUS_CHANGES_REQUESTED
    )
    agent.github.comment_pr.assert_called_once()


def test_not_stale_no_recovery():
    agent = _make_agent()
    fixed_now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    agent._now = lambda: fixed_now

    issue = Issue(number=3, title="t", body="", labels=[agent.STATUS_IMPLEMENTING])
    agent.github.list_issues.return_value = [issue]
    agent.github.list_prs.return_value = []
    agent.github.get_issue_comments.return_value = [
        {"created_at": "2024-01-01T11:50:00Z"}
    ]
    agent.github.get_issue_events.return_value = []

    agent._process_stale_locks()

    agent.github.remove_label.assert_not_called()
    agent.github.add_label.assert_not_called()
    agent.github.comment_issue.assert_not_called()
