"""Tests for shared.lock LockManager."""

from unittest.mock import MagicMock, patch

from shared.lock import LockManager


def test_parse_ack_message_valid() -> None:
    manager = LockManager(MagicMock(), "worker", "agent-1")
    parsed = manager._parse_ack_message("ACK:worker:agent-1:123456")

    assert parsed is not None
    assert parsed["agent_id"] == "agent-1"


def test_parse_ack_message_invalid() -> None:
    manager = LockManager(MagicMock(), "worker", "agent-1")

    assert manager._parse_ack_message("INVALID") is None
    assert manager._parse_ack_message("ACK:worker:agent-1") is None


@patch("shared.lock.time")
def test_get_active_lock_returns_agent(mock_time) -> None:
    mock_time.time.return_value = 1.0
    github = MagicMock()
    github.get_issue_comments.return_value = [{"body": "ACK:worker:agent-1:900"}]

    manager = LockManager(github, "worker", "agent-1")

    assert manager.get_active_lock(42) == "agent-1"


@patch("shared.lock.time")
def test_try_lock_issue_success(mock_time) -> None:
    mock_time.time.side_effect = [1.0, 2.0]
    mock_time.sleep.return_value = None

    github = MagicMock()
    github.comment_issue.return_value = True
    github.remove_label.return_value = True
    github.add_label.return_value = True
    github.get_issue_comments.side_effect = [[], [{"body": "ACK:worker:agent-1:2000"}]]
    github.get_issue.return_value = MagicMock(labels=["status:implementing"])

    manager = LockManager(github, "worker", "agent-1")

    result = manager.try_lock_issue(10, "status:ready", "status:implementing")

    assert result.success is True
    assert result.lock_id == "ACK:worker:agent-1:2000"
    github.remove_label.assert_called_once_with(10, "status:ready")
    github.add_label.assert_called_once_with(10, "status:implementing")


@patch("shared.lock.time")
def test_try_lock_issue_conflict(mock_time) -> None:
    mock_time.time.side_effect = [1.0, 2.0]
    mock_time.sleep.return_value = None

    github = MagicMock()
    github.comment_issue.return_value = True
    github.get_issue_comments.side_effect = [[], [{"body": "ACK:worker:other:1500"}]]

    manager = LockManager(github, "worker", "agent-1")

    result = manager.try_lock_issue(10, "status:ready", "status:implementing")

    assert result.success is False
    assert "Lost lock" in result.error or "Locked by" in result.error


@patch("shared.lock.time")
def test_try_lock_pr_success(mock_time) -> None:
    mock_time.time.side_effect = [1.0, 2.0]
    mock_time.sleep.return_value = None

    github = MagicMock()
    github.comment_pr.return_value = True
    github.remove_pr_label.return_value = True
    github.add_pr_label.return_value = True
    github.get_issue_comments.side_effect = [[], [{"body": "ACK:worker:agent-1:2000"}]]
    github.get_pr.return_value = MagicMock(labels=["status:reviewing"])

    manager = LockManager(github, "worker", "agent-1")

    result = manager.try_lock_pr(24, "status:ready", "status:reviewing")

    assert result.success is True


@patch("shared.lock.time")
def test_try_lock_pr_conflict(mock_time) -> None:
    mock_time.time.side_effect = [1.0, 2.0]
    mock_time.sleep.return_value = None

    github = MagicMock()
    github.comment_pr.return_value = True
    github.get_issue_comments.side_effect = [[], [{"body": "ACK:worker:other:1500"}]]

    manager = LockManager(github, "worker", "agent-1")

    result = manager.try_lock_pr(24, "status:ready", "status:reviewing")

    assert result.success is False


@patch("shared.lock.time")
def test_try_lock_pr_verification_failure(mock_time) -> None:
    mock_time.time.side_effect = [1.0, 2.0]
    mock_time.sleep.return_value = None

    github = MagicMock()
    github.comment_pr.return_value = True
    github.remove_pr_label.return_value = True
    github.add_pr_label.return_value = True
    github.get_issue_comments.side_effect = [[], [{"body": "ACK:worker:agent-1:2000"}]]
    github.get_pr.return_value = MagicMock(labels=["status:ready"])

    manager = LockManager(github, "worker", "agent-1")

    result = manager.try_lock_pr(24, "status:ready", "status:reviewing")

    assert result.success is False
    assert "not found after transition" in (result.error or "")


def test_mark_failed_updates_labels_and_comments() -> None:
    github = MagicMock()
    github.comment_issue.return_value = True

    manager = LockManager(github, "worker", "agent-1")

    assert manager.mark_failed(99, "status:working", "boom") is True
    github.remove_label.assert_called_once_with(99, "status:working")
    github.add_label.assert_called_once_with(99, "status:failed")
    github.comment_issue.assert_called_once()
