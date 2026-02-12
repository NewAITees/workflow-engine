"""TDD tests for issue #35: specification-review gate before status:ready."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from shared.config import AgentConfig
from shared.github_client import Issue


def _load_module(module_name: str, relative_path: str):
    """Load an agent module directly from file path for isolated testing."""
    module_path = Path(__file__).parent.parent / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


planner_main = _load_module("issue35_planner_main", "planner-agent/main.py")
reviewer_main = _load_module("issue35_reviewer_main", "reviewer-agent/main.py")
worker_main = _load_module("issue35_worker_main", "worker-agent/main.py")

PlannerAgent = planner_main.PlannerAgent
ReviewerAgent = reviewer_main.ReviewerAgent
WorkerAgent = worker_main.WorkerAgent


@pytest.fixture
def planner_agent(monkeypatch) -> PlannerAgent:
    """Build a planner agent with mocked external dependencies."""
    config = AgentConfig(repo="owner/repo")
    monkeypatch.setattr(
        planner_main,
        "get_agent_config",
        lambda repo, config_path=None: config,
    )

    agent = PlannerAgent("owner/repo")
    agent.github = MagicMock()
    agent.llm = MagicMock()
    return agent


@pytest.fixture
def reviewer_agent(monkeypatch, tmp_path: Path) -> ReviewerAgent:
    """Build a reviewer agent with mocked GitHub/LLM/lock dependencies."""
    config = AgentConfig(repo="owner/repo")
    monkeypatch.setattr(
        reviewer_main,
        "get_agent_config",
        lambda repo, config_path=None: config,
    )
    monkeypatch.setattr(ReviewerAgent, "ACCUMULATED_DIR", tmp_path / "accumulated")

    agent = ReviewerAgent("owner/repo")
    agent.github = MagicMock()
    agent.lock = MagicMock()
    lock_result = MagicMock()
    lock_result.success = True
    agent.lock.try_lock_issue.return_value = lock_result
    agent.lock.try_lock_pr.return_value = lock_result
    agent.llm = MagicMock()
    return agent


@pytest.fixture
def worker_agent(monkeypatch, tmp_path: Path) -> WorkerAgent:
    """Build a worker agent with mocked runtime services."""
    config = AgentConfig(repo="owner/repo", work_dir=str(tmp_path / "work"))
    monkeypatch.setattr(
        worker_main,
        "get_agent_config",
        lambda repo, config_path=None: config,
    )

    github = MagicMock()
    lock = MagicMock()
    llm = MagicMock()
    git = MagicMock()
    git.path = tmp_path / "repo"
    git.workspace = str(tmp_path / "workspace")
    workspace = MagicMock()

    monkeypatch.setattr(worker_main, "GitHubClient", lambda repo, gh_cli="gh": github)
    monkeypatch.setattr(
        worker_main,
        "LockManager",
        lambda gh, agent_type, agent_id: lock,
    )
    monkeypatch.setattr(worker_main, "LLMClient", lambda cfg: llm)
    monkeypatch.setattr(worker_main, "GitOperations", lambda repo, path: git)
    monkeypatch.setattr(
        worker_main,
        "WorkspaceManager",
        lambda repo, path: workspace,
    )

    agent = WorkerAgent("owner/repo")
    agent._process_stale_locks = MagicMock(return_value=False)
    return agent


def test_planner_create_spec_starts_with_spec_review(
    planner_agent: PlannerAgent,
) -> None:
    """Planner-created issues must start at status:spec-review, not status:ready."""
    planner_agent.llm.create_spec.return_value = MagicMock(
        success=True,
        output="## Specification\n\nDetails",
    )
    planner_agent.github.create_issue.return_value = 35

    result = planner_agent.create_spec("As a user, I want a clearer review gate")

    assert result is not None
    assert "/issues/35" in result
    planner_agent.github.create_issue.assert_called_once()
    labels = planner_agent.github.create_issue.call_args.kwargs["labels"]
    assert labels == ["status:spec-review"]


def test_planner_revised_spec_returns_to_spec_review(
    planner_agent: PlannerAgent,
) -> None:
    """After planner rework from escalation, issue must go back to spec-review."""
    planner_agent.llm.create_spec.return_value = MagicMock(
        success=True,
        output="## Revised specification",
    )
    planner_agent.github.get_issue.return_value = Issue(
        number=35,
        title="Issue",
        body="Current spec",
        labels=["status:escalated"],
    )
    planner_agent.github.get_issue_comments.return_value = [
        {
            "body": "ESCALATION:worker\nNeed clearer acceptance criteria.",
            "created_at": "2026-02-11T10:00:00Z",
        }
    ]
    planner_agent.github.update_issue_body.return_value = True

    assert planner_agent._try_process_escalated_issue(35) is True

    assert any(
        call.args == (35, "status:spec-review")
        for call in planner_agent.github.add_label.call_args_list
    )
    assert not any(
        call.args == (35, planner_agent.STATUS_READY)
        for call in planner_agent.github.add_label.call_args_list
    )


def test_reviewer_run_once_processes_spec_review_issues_first(
    reviewer_agent: ReviewerAgent,
) -> None:
    """Reviewer run_once should handle spec-review issues before PR code review."""
    issue = Issue(
        number=35,
        title="Spec review target",
        body="Specification body",
        labels=["status:spec-review"],
    )
    reviewer_agent.github.list_issues.return_value = [issue]
    reviewer_agent.github.list_prs.return_value = []
    reviewer_agent._try_review_spec_issue = MagicMock(return_value=True)
    reviewer_agent._try_review_pr = MagicMock(return_value=True)

    result = reviewer_agent.run_once()

    assert result is True
    reviewer_agent.github.list_issues.assert_called_once_with(
        labels=["status:spec-review"]
    )
    reviewer_agent._try_review_spec_issue.assert_called_once_with(issue)
    reviewer_agent.github.list_prs.assert_not_called()
    reviewer_agent._try_review_pr.assert_not_called()


def test_reviewer_spec_review_ok_transitions_to_ready(
    reviewer_agent: ReviewerAgent,
) -> None:
    """Approved spec review should move issue from spec-review to ready."""
    issue = Issue(
        number=35,
        title="Spec review target",
        body="Specification body",
        labels=["status:spec-review"],
    )
    review_payload = {
        "decision": "approve",
        "summary": "Specification is implementation-ready.",
        "issues": [],
    }
    llm_result = MagicMock(success=True, output=json.dumps(review_payload))
    reviewer_agent.llm.review_spec.return_value = llm_result

    assert reviewer_agent._try_review_spec_issue(issue) is True

    reviewer_agent.github.remove_label.assert_any_call(
        issue.number, "status:spec-review"
    )
    reviewer_agent.github.add_label.assert_any_call(issue.number, "status:ready")
    combined_comments = "\n".join(
        str(call.args[1]) for call in reviewer_agent.github.comment_issue.call_args_list
    )
    assert "SPEC_REVIEW:APPROVED" in combined_comments


def test_reviewer_spec_review_ng_adds_escalation_and_rework_guidance(
    reviewer_agent: ReviewerAgent,
) -> None:
    """Rejected spec review should escalate to planner with explicit re-review path."""
    issue = Issue(
        number=35,
        title="Spec review target",
        body="Specification body",
        labels=["status:spec-review"],
    )
    review_payload = {
        "decision": "changes_requested",
        "summary": "Acceptance criteria are ambiguous.",
        "issues": [
            {
                "severity": "major",
                "description": "Expected behavior for edge case is missing.",
            }
        ],
    }
    llm_result = MagicMock(success=True, output=json.dumps(review_payload))
    reviewer_agent.llm.review_spec.return_value = llm_result

    assert reviewer_agent._try_review_spec_issue(issue) is True

    assert any(
        call.args == (issue.number, "status:escalated")
        for call in reviewer_agent.github.add_label.call_args_list
    )
    assert not any(
        call.args == (issue.number, "status:ready")
        for call in reviewer_agent.github.add_label.call_args_list
    )
    combined_comments = "\n".join(
        str(call.args[1]) for call in reviewer_agent.github.comment_issue.call_args_list
    )
    assert "ESCALATION:reviewer" in combined_comments
    assert "status:spec-review" in combined_comments


def test_worker_remains_ready_only_after_gate_introduction(
    worker_agent: WorkerAgent,
) -> None:
    """Worker must continue to process only status:ready issues."""
    worker_agent.github.list_issues.return_value = []
    worker_agent.github.list_prs.return_value = []

    assert worker_agent.run_once() is False

    worker_agent.github.list_issues.assert_called_once_with(
        labels=[worker_agent.STATUS_READY]
    )
