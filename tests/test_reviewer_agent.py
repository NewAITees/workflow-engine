"""Tests for reviewer agent auto-merge behavior."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import AgentConfig
from shared.github_client import PullRequest
from shared.lock import LockResult


def _load_reviewer_module():
    module_path = Path(__file__).parent.parent / "reviewer-agent" / "main.py"
    spec = importlib.util.spec_from_file_location("reviewer_agent_main", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["reviewer_agent_main"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_pr() -> PullRequest:
    return PullRequest(
        number=1,
        title="Test PR",
        body="No linked issue",
        labels=["status:reviewing"],
        head_ref="feature",
        base_ref="main",
    )


def _setup_mocks(mock_github, mock_lock, mock_llm):
    mock_lock.try_lock_pr.return_value = LockResult(success=True)
    mock_github.get_pr_diff.return_value = "diff"
    mock_github.remove_pr_label.return_value = True
    mock_github.add_pr_label.return_value = True
    mock_github.approve_pr.return_value = True
    mock_github.merge_pr.return_value = True
    mock_llm.review_code.return_value = SimpleNamespace(
        success=True, output="DECISION: APPROVE"
    )


def test_auto_merge_disabled_skips_merge():
    reviewer_main = _load_reviewer_module()

    mock_github = MagicMock()
    mock_lock = MagicMock()
    mock_llm = MagicMock()
    _setup_mocks(mock_github, mock_lock, mock_llm)

    config = AgentConfig(repo="owner/repo", auto_merge=False)

    with (
        patch.object(reviewer_main, "GitHubClient", return_value=mock_github),
        patch.object(reviewer_main, "LockManager", return_value=mock_lock),
        patch.object(reviewer_main, "LLMClient", return_value=mock_llm),
        patch.object(reviewer_main, "get_agent_config", return_value=config),
    ):
        agent = reviewer_main.ReviewerAgent("owner/repo")
        assert agent._try_review_pr(_make_pr()) is True

    mock_github.merge_pr.assert_not_called()


def test_auto_merge_uses_configured_method():
    reviewer_main = _load_reviewer_module()

    mock_github = MagicMock()
    mock_lock = MagicMock()
    mock_llm = MagicMock()
    _setup_mocks(mock_github, mock_lock, mock_llm)

    config = AgentConfig(repo="owner/repo", auto_merge=True, merge_method="rebase")

    with (
        patch.object(reviewer_main, "GitHubClient", return_value=mock_github),
        patch.object(reviewer_main, "LockManager", return_value=mock_lock),
        patch.object(reviewer_main, "LLMClient", return_value=mock_llm),
        patch.object(reviewer_main, "get_agent_config", return_value=config),
    ):
        agent = reviewer_main.ReviewerAgent("owner/repo")
        assert agent._try_review_pr(_make_pr()) is True

    mock_github.merge_pr.assert_called_once_with(1, method="rebase")


def test_auto_merge_skipped_when_approval_fails():
    reviewer_main = _load_reviewer_module()

    mock_github = MagicMock()
    mock_lock = MagicMock()
    mock_llm = MagicMock()
    _setup_mocks(mock_github, mock_lock, mock_llm)
    mock_github.approve_pr.return_value = False

    config = AgentConfig(repo="owner/repo", auto_merge=True)

    with (
        patch.object(reviewer_main, "GitHubClient", return_value=mock_github),
        patch.object(reviewer_main, "LockManager", return_value=mock_lock),
        patch.object(reviewer_main, "LLMClient", return_value=mock_llm),
        patch.object(reviewer_main, "get_agent_config", return_value=config),
    ):
        agent = reviewer_main.ReviewerAgent("owner/repo")
        assert agent._try_review_pr(_make_pr()) is True

    mock_github.merge_pr.assert_not_called()
    mock_github.add_pr_label.assert_not_called()


def test_auto_merge_skipped_when_label_update_fails():
    reviewer_main = _load_reviewer_module()

    mock_github = MagicMock()
    mock_lock = MagicMock()
    mock_llm = MagicMock()
    _setup_mocks(mock_github, mock_lock, mock_llm)
    mock_github.add_pr_label.return_value = False

    config = AgentConfig(repo="owner/repo", auto_merge=True)

    with (
        patch.object(reviewer_main, "GitHubClient", return_value=mock_github),
        patch.object(reviewer_main, "LockManager", return_value=mock_lock),
        patch.object(reviewer_main, "LLMClient", return_value=mock_llm),
        patch.object(reviewer_main, "get_agent_config", return_value=config),
    ):
        agent = reviewer_main.ReviewerAgent("owner/repo")
        assert agent._try_review_pr(_make_pr()) is True

    mock_github.merge_pr.assert_not_called()
    mock_github.comment_pr.assert_called_once()
