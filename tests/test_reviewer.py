"""Tests for ReviewerAgent severity-based review handling."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from shared.config import AgentConfig
from shared.github_client import Issue, PullRequest

spec = importlib.util.spec_from_file_location(
    "reviewer_main",
    Path(__file__).parent.parent / "reviewer-agent" / "main.py",
)
if spec is None or spec.loader is None:
    raise ImportError("Failed to load reviewer-agent/main.py")
reviewer_main = importlib.util.module_from_spec(spec)
sys.modules["reviewer_main"] = reviewer_main
spec.loader.exec_module(reviewer_main)

ReviewerAgent = reviewer_main.ReviewerAgent


@pytest.fixture
def reviewer_agent(monkeypatch, tmp_path: Path) -> ReviewerAgent:
    config = AgentConfig(repo="owner/repo")
    monkeypatch.setattr(
        reviewer_main, "get_agent_config", lambda repo, config_path=None: config
    )
    monkeypatch.setattr(ReviewerAgent, "ACCUMULATED_DIR", tmp_path / "accumulated")
    monkeypatch.setattr(ReviewerAgent, "ACCUMULATED_THRESHOLD", 2)

    agent = ReviewerAgent("owner/repo")
    agent.github = MagicMock()
    agent.lock = MagicMock()
    lock_result = MagicMock()
    lock_result.success = True
    agent.lock.try_lock_pr.return_value = lock_result
    agent.llm = MagicMock()
    agent._find_linked_issue = MagicMock(
        return_value=Issue(
            number=11,
            title="Issue title",
            body="Full specification",
            labels=["status:ready"],
            state="open",
        )
    )
    return agent


def _make_pr() -> PullRequest:
    return PullRequest(
        number=1,
        title="Test PR",
        body="Closes #11",
        labels=["status:reviewing"],
        head_ref="feature",
        base_ref="main",
        state="open",
    )


def _make_review_payload(severity: str) -> dict:
    return {
        "overall_decision": "request_changes",
        "issues": [
            {
                "severity": severity,
                "file": "app.py",
                "line": 10,
                "description": f"{severity} issue",
                "suggestion": "Fix it",
            }
        ],
        "summary": f"{severity} issue summary",
    }


def test_severity_classification_critical_immediate_feedback(
    reviewer_agent: ReviewerAgent,
) -> None:
    pr = _make_pr()
    reviewer_agent.github.get_pr_diff.return_value = "diff"
    reviewer_agent.llm.review_code_with_severity.return_value = MagicMock(
        success=True,
        output=json.dumps(_make_review_payload("critical")),
    )

    assert reviewer_agent._try_review_pr(pr) is True

    reviewer_agent.github.request_changes_pr.assert_called_once()
    reviewer_agent.github.add_pr_label.assert_any_call(
        pr.number, reviewer_agent.STATUS_CHANGES_REQUESTED
    )


def test_severity_classification_major_immediate_feedback(
    reviewer_agent: ReviewerAgent,
) -> None:
    pr = _make_pr()
    reviewer_agent.github.get_pr_diff.return_value = "diff"
    reviewer_agent.llm.review_code_with_severity.return_value = MagicMock(
        success=True,
        output=json.dumps(_make_review_payload("major")),
    )

    assert reviewer_agent._try_review_pr(pr) is True

    reviewer_agent.github.request_changes_pr.assert_called_once()
    reviewer_agent.github.add_pr_label.assert_any_call(
        pr.number, reviewer_agent.STATUS_CHANGES_REQUESTED
    )


def test_severity_classification_minor_accumulate(
    reviewer_agent: ReviewerAgent,
) -> None:
    pr = _make_pr()
    reviewer_agent.github.get_pr_diff.return_value = "diff"
    reviewer_agent.llm.review_code_with_severity.return_value = MagicMock(
        success=True,
        output=json.dumps(_make_review_payload("minor")),
    )

    assert reviewer_agent._try_review_pr(pr) is True

    reviewer_agent.github.comment_pr.assert_called_once()
    reviewer_agent.github.approve_pr.assert_called_once()

    fix_file = reviewer_agent.accumulated_fixes_dir / f"pr-{pr.number}.json"
    data = json.loads(fix_file.read_text())
    assert data["current_count"] == 1


def test_severity_classification_trivial_accumulate(
    reviewer_agent: ReviewerAgent,
) -> None:
    pr = _make_pr()
    reviewer_agent.github.get_pr_diff.return_value = "diff"
    reviewer_agent.llm.review_code_with_severity.return_value = MagicMock(
        success=True,
        output=json.dumps(_make_review_payload("trivial")),
    )

    assert reviewer_agent._try_review_pr(pr) is True

    reviewer_agent.github.comment_pr.assert_called_once()
    reviewer_agent.github.approve_pr.assert_called_once()

    fix_file = reviewer_agent.accumulated_fixes_dir / f"pr-{pr.number}.json"
    data = json.loads(fix_file.read_text())
    assert data["current_count"] == 1
    assert all(issue["severity"] == "trivial" for issue in data["accumulated_issues"])


def test_accumulated_threshold_reached(reviewer_agent: ReviewerAgent) -> None:
    pr_number = 7
    issue_data = _make_review_payload("minor")["issues"][0]

    assert (
        reviewer_agent._add_accumulated_issue(pr_number, issue_data, issue_number=11)
        is False
    )
    assert (
        reviewer_agent._add_accumulated_issue(pr_number, issue_data, issue_number=11)
        is True
    )

    fix_file = reviewer_agent.accumulated_fixes_dir / f"pr-{pr_number}.json"
    assert fix_file.exists()

    reviewer_agent._clear_accumulated_fixes(pr_number)
    assert not fix_file.exists()


def test_accumulated_fixes_storage(
    reviewer_agent: ReviewerAgent, tmp_path: Path
) -> None:
    pr_number = 8
    payload = _make_review_payload("minor")["issues"][0]
    reviewer_agent._add_accumulated_issue(pr_number, payload, issue_number=11)

    data = reviewer_agent._load_accumulated_fixes(pr_number)
    assert data["issue_number"] == 11
    assert data["threshold"] == reviewer_agent.ACCUMULATED_THRESHOLD
    assert data["current_count"] == 1


def test_mixed_severity_critical_wins(reviewer_agent: ReviewerAgent) -> None:
    pr = _make_pr()
    reviewer_agent.github.get_pr_diff.return_value = "diff"
    payload = {
        "overall_decision": "request_changes",
        "issues": [
            {
                "severity": "critical",
                "file": "src/app.py",
                "line": 1,
                "description": "critical issue",
                "suggestion": "fix",
            },
            {
                "severity": "minor",
                "file": "src/app.py",
                "line": 2,
                "description": "minor issue",
                "suggestion": "tweak",
            },
        ],
        "summary": "Mixed severity",
    }
    reviewer_agent.llm.review_code_with_severity.return_value = MagicMock(
        success=True,
        output=json.dumps(payload),
    )

    assert reviewer_agent._try_review_pr(pr) is True

    reviewer_agent.github.request_changes_pr.assert_called_once()
    reviewer_agent.github.comment_pr.assert_not_called()


def test_no_issues_approve(reviewer_agent: ReviewerAgent) -> None:
    pr = _make_pr()
    reviewer_agent.github.get_pr_diff.return_value = "diff"
    reviewer_agent.llm.review_code_with_severity.return_value = MagicMock(
        success=True,
        output=json.dumps(
            {
                "overall_decision": "approve",
                "issues": [],
                "summary": "Looks good",
            }
        ),
    )

    assert reviewer_agent._try_review_pr(pr) is True

    reviewer_agent.github.approve_pr.assert_called_once()
    reviewer_agent.github.request_changes_pr.assert_not_called()
