"""Tests for lock mechanism."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.github_client import GitHubClient
from shared.lock import LockManager


class TestLockManager:
    """Tests for LockManager."""

    def setup_method(self):
        """Set up test fixtures."""
        self.github = MagicMock(spec=GitHubClient)
        self.lock = LockManager(self.github, "worker", "test-agent-123")

    def test_parse_ack_message_valid(self):
        """Test parsing a valid ACK message."""
        msg = "ACK:worker:agent-123:1234567890"
        result = self.lock._parse_ack_message(msg)

        assert result is not None
        assert result["agent_type"] == "worker"
        assert result["agent_id"] == "agent-123"
        assert result["timestamp"] == 1234567890

    def test_parse_ack_message_invalid_format(self):
        """Test parsing invalid ACK message formats."""
        # Not starting with ACK:
        assert self.lock._parse_ack_message("NACK:worker:agent:123") is None

        # Wrong number of parts
        assert self.lock._parse_ack_message("ACK:worker:agent") is None
        assert self.lock._parse_ack_message("ACK:worker:agent:123:extra") is None

        # Empty message
        assert self.lock._parse_ack_message("") is None

    def test_parse_ack_message_malformed_timestamp(self):
        """Test parsing ACK with non-integer timestamp."""
        msg = "ACK:worker:agent-123:not-a-number"
        result = self.lock._parse_ack_message(msg)
        assert result is None

    def test_parse_ack_message_empty_timestamp(self):
        """Test parsing ACK with empty timestamp."""
        msg = "ACK:worker:agent-123:"
        result = self.lock._parse_ack_message(msg)
        assert result is None

    def test_lock_filters_old_acks(self):
        """Test that old ACKs outside time window are ignored."""
        current_time = int(time.time() * 1000)
        old_time = current_time - 60000  # 60 seconds ago (outside 30s window)

        # Mock comment posting success
        self.github.comment_issue.return_value = True

        # Return an old ACK that should be filtered out
        self.github.get_issue_comments.return_value = [
            {"body": f"ACK:worker:old-agent:{old_time}"},
        ]

        # Mock label operations
        self.github.remove_label.return_value = True
        self.github.add_label.return_value = True
        self.github.get_issue.return_value = MagicMock(labels=["status:implementing"])

        with patch("time.sleep"):
            with patch("time.time", return_value=current_time / 1000):
                result = self.lock.try_lock_issue(
                    1, "status:ready", "status:implementing"
                )

        # Should fail because our ACK isn't in the comments (only old one)
        # The old ACK is filtered, so no valid ACKs found
        assert result.success is False
        assert "ACK comment not found" in result.error

    def test_lock_winner_is_earliest_in_window(self):
        """Test that the earliest ACK within time window wins."""
        current_time = int(time.time() * 1000)
        earlier_time = current_time - 1000  # 1 second earlier

        self.github.comment_issue.return_value = True

        # Another agent posted slightly earlier
        self.github.get_issue_comments.return_value = [
            {"body": f"ACK:worker:other-agent:{earlier_time}"},
            {"body": f"ACK:worker:test-agent-123:{current_time}"},
        ]

        with patch("time.sleep"):
            with patch("time.time", return_value=current_time / 1000):
                result = self.lock.try_lock_issue(
                    1, "status:ready", "status:implementing"
                )

        assert result.success is False
        assert "Lost lock to other-agent" in result.error

    def test_lock_success_when_first(self):
        """Test successful lock when we're the first ACK."""
        current_time = int(time.time() * 1000)

        self.github.comment_issue.return_value = True
        self.github.get_issue_comments.return_value = [
            {"body": f"ACK:worker:test-agent-123:{current_time}"},
        ]
        self.github.remove_label.return_value = True
        self.github.add_label.return_value = True
        self.github.get_issue.return_value = MagicMock(labels=["status:implementing"])

        with patch("time.sleep"):
            with patch("time.time", return_value=current_time / 1000):
                result = self.lock.try_lock_issue(
                    1, "status:ready", "status:implementing"
                )

        assert result.success is True
        assert result.lock_id is not None

    def test_lock_ignores_other_agent_types(self):
        """Test that ACKs from different agent types are ignored."""
        current_time = int(time.time() * 1000)
        earlier_time = current_time - 1000

        self.github.comment_issue.return_value = True

        # Reviewer agent ACK should be ignored by worker lock
        self.github.get_issue_comments.return_value = [
            {"body": f"ACK:reviewer:reviewer-agent:{earlier_time}"},
            {"body": f"ACK:worker:test-agent-123:{current_time}"},
        ]
        self.github.remove_label.return_value = True
        self.github.add_label.return_value = True
        self.github.get_issue.return_value = MagicMock(labels=["status:implementing"])

        with patch("time.sleep"):
            with patch("time.time", return_value=current_time / 1000):
                result = self.lock.try_lock_issue(
                    1, "status:ready", "status:implementing"
                )

        # Should succeed because reviewer ACK doesn't compete with worker
        assert result.success is True
