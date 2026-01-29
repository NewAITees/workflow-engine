"""Tests for git operations."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "worker-agent"))

from shared.git_operations import GitOperations, GitResult


class TestGitOperations:
    """Tests for GitOperations."""

    def setup_method(self):
        """Set up test fixtures."""
        self.git = GitOperations("owner/repo", Path("/tmp/test-workspaces"))

    @patch("subprocess.run")
    def test_run_success(self, mock_run):
        """Test successful command execution."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="success output",
            stderr="",
        )

        result = self.git._run(["status"])

        assert result.success is True
        assert result.output == "success output"
        assert result.error is None

    @patch("subprocess.run")
    def test_run_failure_with_check_true(self, mock_run):
        """Test command failure with check=True raises and returns failure."""
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["git", "status"],
        )
        error.stdout = ""
        error.stderr = "error message"
        mock_run.side_effect = error

        result = self.git._run(["status"], check=True)

        assert result.success is False
        assert result.error == "error message"

    @patch("subprocess.run")
    def test_run_failure_with_check_false(self, mock_run):
        """Test command failure with check=False returns failure correctly."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="partial output",
            stderr="error message",
        )

        result = self.git._run(["checkout", "nonexistent"], check=False)

        assert result.success is False
        assert result.output == "partial output"
        assert result.error == "error message"

    @patch("subprocess.run")
    def test_run_failure_with_check_false_no_stderr(self, mock_run):
        """Test failure with check=False and no stderr message."""
        mock_run.return_value = MagicMock(
            returncode=128,
            stdout="",
            stderr="",
        )

        result = self.git._run(["rev-parse", "HEAD"], check=False)

        assert result.success is False
        assert "Exit code: 128" in result.error

    @patch("subprocess.run")
    def test_get_default_branch_success(self, mock_run):
        """Test getting default branch successfully."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="refs/remotes/origin/main\n",
        )

        branch = self.git.get_default_branch()

        assert branch == "main"

    @patch("subprocess.run")
    def test_get_default_branch_master(self, mock_run):
        """Test getting master as default branch."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="refs/remotes/origin/master\n",
        )

        branch = self.git.get_default_branch()

        assert branch == "master"

    @patch("subprocess.run")
    def test_get_default_branch_fallback(self, mock_run):
        """Test fallback when symbolic-ref fails."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="fatal: ref refs/remotes/origin/HEAD is not a symbolic ref",
        )

        branch = self.git.get_default_branch()

        assert branch == "main"  # Fallback

    @patch.object(GitOperations, "_run")
    @patch.object(GitOperations, "get_default_branch")
    def test_clone_or_pull_existing_repo(self, mock_default_branch, mock_run):
        """Test updating existing repository."""
        mock_default_branch.return_value = "main"
        mock_run.return_value = GitResult(success=True, output="Already up to date.")

        # Simulate existing workspace
        with patch.object(Path, "exists", return_value=True):
            result = self.git.clone_or_pull()

        assert result.success is True
        # Should have called fetch, checkout, reset, clean, pull
        assert mock_run.call_count >= 4

    @patch.object(GitOperations, "_run")
    def test_clone_or_pull_new_repo(self, mock_run):
        """Test cloning new repository."""
        mock_run.return_value = GitResult(success=True, output="Cloning...")

        # Simulate non-existing workspace
        with patch.object(Path, "exists", return_value=False):
            result = self.git.clone_or_pull()

        assert result.success is True
        # Should have called clone
        call_args = mock_run.call_args_list[-1]
        assert "clone" in call_args[0][0]

    @patch.object(GitOperations, "_run")
    def test_commit_no_changes(self, mock_run):
        """Test commit when there are no changes."""
        # stage_all succeeds
        mock_run.side_effect = [
            GitResult(success=True, output=""),  # add -A
            GitResult(
                success=True, output=""
            ),  # status --porcelain (empty = no changes)
        ]

        result = self.git.commit("test commit")

        assert result.success is False
        assert "No changes to commit" in result.error

    @patch.object(GitOperations, "_run")
    def test_commit_with_changes(self, mock_run):
        """Test commit when there are changes."""
        mock_run.side_effect = [
            GitResult(success=True, output=""),  # add -A
            GitResult(success=True, output="M  file.py\n"),  # status --porcelain
            GitResult(success=True, output="[main abc123] test commit"),  # commit
        ]

        result = self.git.commit("test commit")

        assert result.success is True
