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

    @patch("subprocess.run")
    def test_get_default_branch_fallback(self, mock_run):
        """Test default branch fallback when API call fails."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        branch = self.client.get_default_branch()

        assert branch == "main"

    @patch("subprocess.run")
    def test_get_issue_success(self, mock_run):
        """Test retrieving a single issue."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 5,
                    "title": "Issue title",
                    "body": "Details",
                    "labels": [{"name": "status:ready"}],
                    "state": "open",
                }
            ),
        )

        issue = self.client.get_issue(5)

        assert issue is not None
        assert issue.number == 5
        assert "status:ready" in issue.labels

    @patch("subprocess.run")
    def test_get_issue_not_found(self, mock_run):
        """Test get_issue returns None when not found."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Not found")

        assert self.client.get_issue(99) is None

    @patch("subprocess.run")
    def test_remove_label(self, mock_run):
        """Test removing a label success and failure."""
        mock_run.return_value = MagicMock(returncode=0)
        assert self.client.remove_label(1, "status:ready") is True

        mock_run.return_value = MagicMock(returncode=1)
        assert self.client.remove_label(1, "status:ready") is False

    @patch("subprocess.run")
    def test_comment_issue(self, mock_run):
        """Test commenting on an issue."""
        mock_run.return_value = MagicMock(returncode=0)
        assert self.client.comment_issue(1, "message") is True

        mock_run.return_value = MagicMock(returncode=1)
        assert self.client.comment_issue(1, "message") is False

    @patch("subprocess.run")
    def test_update_issue_body(self, mock_run):
        """Test updating issue body."""
        mock_run.return_value = MagicMock(returncode=0)
        assert self.client.update_issue_body(1, "new body") is True

        mock_run.return_value = MagicMock(returncode=1)
        assert self.client.update_issue_body(1, "new body") is False

    @patch("subprocess.run")
    def test_get_issue_comments_parses_valid_json(self, mock_run):
        """Test parsing multiple comments with mixed validity."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"id":1,"body":"ACK"}\ninvalid\n{"id":2,"body":"ACK"}\n',
        )

        comments = self.client.get_issue_comments(1, limit=5)

        assert len(comments) == 2
        assert comments[0]["id"] == 1

    @patch("subprocess.run")
    def test_create_issue_parses_number(self, mock_run):
        """Test create_issue returns issue number from URL."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/owner/repo/issues/7\n"
        )

        assert self.client.create_issue("title", "body") == 7

    @patch("subprocess.run")
    def test_create_issue_malformed_url(self, mock_run):
        """Test create_issue handles unexpected output."""
        mock_run.return_value = MagicMock(returncode=0, stdout="not-a-url")

        assert self.client.create_issue("title", "body") is None

    @patch("subprocess.run")
    def test_list_prs_with_labels(self, mock_run):
        """Test listing PRs and parsing labels."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 3,
                        "title": "Add feature",
                        "body": "",
                        "labels": [{"name": "status:reviewing"}],
                        "headRefName": "feature",
                        "baseRefName": "main",
                        "state": "open",
                    }
                ]
            ),
        )

        prs = self.client.list_prs(labels=["status:reviewing"])

        assert prs and prs[0].head_ref == "feature"
        assert "status:reviewing" in prs[0].labels

    @patch("subprocess.run")
    def test_get_pr_not_found(self, mock_run):
        """Test get_pr returns None when gh fails."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        assert self.client.get_pr(99) is None

    @patch("subprocess.run")
    def test_get_pr_checks_no_ci(self, mock_run):
        """Test get_pr_checks distinguishes empty checks as unknown/no-checks."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        result = self.client.get_pr_checks(1)

        assert result["all_passed"] is False
        assert result["has_checks"] is False

    @patch("subprocess.run")
    def test_get_pr_reviews_filters_invalid_json(self, mock_run):
        """Test get_pr_reviews ignores malformed lines."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"id":1,"state":"APPROVED"}\ninvalid\n{"id":2,"state":"CHANGES_REQUESTED"}\n',
        )

        reviews = self.client.get_pr_reviews(1)

        assert len(reviews) == 2

    @patch("subprocess.run")
    def test_get_ci_status_success(self, mock_run):
        """Test get_ci_status when all checks passed."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='{"headRefOid":"abc123"}'),
            MagicMock(
                returncode=0,
                stdout='{"name":"test","status":"completed","conclusion":"success"}\n'
                '{"name":"lint","status":"completed","conclusion":"success"}\n',
            ),
        ]

        status = self.client.get_ci_status(1)

        assert status["status"] == "success"
        assert status["conclusion"] == "success"
        assert status["failed_count"] == 0
        assert status["pending_count"] == 0
        assert len(status["checks"]) == 2

    @patch("subprocess.run")
    def test_get_ci_status_failure(self, mock_run):
        """Test get_ci_status when some checks failed."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='{"headRefOid":"abc123"}'),
            MagicMock(
                returncode=0,
                stdout='{"name":"test","status":"completed","conclusion":"failure"}\n'
                '{"name":"lint","status":"completed","conclusion":"success"}\n',
            ),
        ]

        status = self.client.get_ci_status(1)

        assert status["status"] == "failure"
        assert status["conclusion"] == "failure"
        assert status["failed_count"] == 1
        assert status["pending_count"] == 0

    @patch("subprocess.run")
    def test_get_ci_status_pending(self, mock_run):
        """Test get_ci_status when checks are pending."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='{"headRefOid":"abc123"}'),
            MagicMock(
                returncode=0,
                stdout='{"name":"test","status":"in_progress","conclusion":null}\n'
                '{"name":"lint","status":"completed","conclusion":"success"}\n',
            ),
        ]

        status = self.client.get_ci_status(1)

        assert status["status"] == "pending"
        assert status["conclusion"] == "pending"
        assert status["pending_count"] == 1

    @patch("subprocess.run")
    def test_get_ci_status_no_checks(self, mock_run):
        """Test get_ci_status when no CI configured."""
        mock_run.side_effect = [MagicMock(returncode=1, stdout="")]

        status = self.client.get_ci_status(1)

        assert status["status"] == "none"
        assert status["conclusion"] == "none"
        assert status["failed_count"] == 0

    @patch("subprocess.run")
    def test_get_ci_logs_with_failures(self, mock_run):
        """Test get_ci_logs returns failed check details."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='{"headRefOid":"abc123"}'),
            MagicMock(
                returncode=0,
                stdout='{"name":"test","conclusion":"failure","html_url":"https://example.com",'
                '"output":{"title":"Test failed","summary":"Error details"}}\n',
            ),
        ]

        logs = self.client.get_ci_logs(1)

        assert len(logs) == 1
        assert logs[0]["name"] == "test"
        assert logs[0]["conclusion"] == "failure"
        assert logs[0]["output"]["title"] == "Test failed"

    @patch("subprocess.run")
    def test_get_ci_logs_no_failures(self, mock_run):
        """Test get_ci_logs returns empty when no failures."""
        mock_run.side_effect = [MagicMock(returncode=1, stdout="")]

        logs = self.client.get_ci_logs(1)

        assert logs == []

    @patch("subprocess.run")
    def test_get_ci_logs_invalid_json(self, mock_run):
        """Test get_ci_logs handles invalid JSON gracefully."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='{"headRefOid":"abc123"}'),
            MagicMock(returncode=0, stdout="invalid json\n"),
        ]

        logs = self.client.get_ci_logs(1)

        assert logs == []

    @patch("subprocess.run")
    def test_get_pr_head_sha_success(self, mock_run):
        """Test get_pr_head_sha returns commit sha when available."""
        mock_run.return_value = MagicMock(returncode=0, stdout='{"headRefOid":"deadbeef"}')

        sha = self.client.get_pr_head_sha(123)

        assert sha == "deadbeef"
