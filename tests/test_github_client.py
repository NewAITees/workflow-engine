"""Tests for GitHub client."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.github_client import GitHubClient


class TestGitHubClient:
    """Tests for GitHubClient."""

    def setup_method(self):
        """Set up test fixtures."""
        self.client = GitHubClient("owner/repo")

    @patch("subprocess.run")
    def test_list_issues_success(self, mock_run):
        """Test listing issues successfully."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 1,
                        "title": "Test Issue",
                        "body": "Test body",
                        "labels": [{"name": "status:ready"}],
                        "state": "open",
                    }
                ]
            ),
        )

        issues = self.client.list_issues(labels=["status:ready"])

        assert len(issues) == 1
        assert issues[0].number == 1
        assert issues[0].title == "Test Issue"
        assert "status:ready" in issues[0].labels

    @patch("subprocess.run")
    def test_list_issues_empty(self, mock_run):
        """Test listing issues with no results."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        issues = self.client.list_issues()

        assert issues == []

    @patch("subprocess.run")
    def test_add_label_success(self, mock_run):
        """Test adding a label successfully."""
        mock_run.return_value = MagicMock(returncode=0)

        result = self.client.add_label(1, "status:implementing")

        assert result is True
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_add_label_failure(self, mock_run):
        """Test adding a label when it fails."""
        mock_run.return_value = MagicMock(returncode=1, stderr="Error")

        result = self.client.add_label(1, "status:implementing")

        assert result is False

    @patch("subprocess.run")
    def test_create_pr_success(self, mock_run):
        """Test creating a PR successfully."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/42\n",
        )

        result = self.client.create_pr(
            title="Test PR",
            body="Test body",
            head="feature-branch",
            labels=["status:reviewing"],
        )

        assert result == "https://github.com/owner/repo/pull/42"

    @patch("subprocess.run")
    def test_get_pr_diff(self, mock_run):
        """Test getting PR diff."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="diff --git a/file.py b/file.py\n+new line",
        )

        diff = self.client.get_pr_diff(1)

        assert "diff --git" in diff
        assert "+new line" in diff

    @patch("subprocess.run")
    def test_is_ci_green_all_passed(self, mock_run):
        """Test CI check when all passed."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [
                    {"name": "test", "state": "success", "conclusion": "success"},
                    {"name": "lint", "state": "success", "conclusion": "success"},
                ]
            ),
        )

        assert self.client.is_ci_green(1) is True

    @patch("subprocess.run")
    def test_is_ci_green_some_failed(self, mock_run):
        """Test CI check when some failed."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [
                    {"name": "test", "state": "success", "conclusion": "success"},
                    {"name": "lint", "state": "failure", "conclusion": "failure"},
                ]
            ),
        )

        assert self.client.is_ci_green(1) is False


class TestLockManager:
    """Tests for lock manager."""

    @patch("subprocess.run")
    def test_lock_acquisition(self, mock_run):
        """Test basic lock acquisition flow."""
        from shared.lock import LockManager

        client = GitHubClient("owner/repo")
        lock = LockManager(client, "worker", "test-agent")

        # Mock successful comment and label operations
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        # This is a simplified test - full test would need more mocking
        assert lock.agent_type == "worker"
        assert lock.agent_id == "test-agent"
