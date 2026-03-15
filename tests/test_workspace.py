"""Tests for workspace management."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from shared.git_operations import GitResult
from shared.workspace import WorkspaceManager


class TestWorkspaceManager:
    """Tests for WorkspaceManager."""

    def setup_method(self):
        self.repo = "owner/repo"
        self.main_work_dir = Path("/tmp/workspaces/owner_repo")
        self.manager = WorkspaceManager(self.repo, self.main_work_dir)

    @patch("shared.workspace.GitOperations")
    def test_ensure_main_repo_clones_if_missing(self, mock_git_cls):
        """Test that main repo is cloned if it doesn't exist."""
        # Setup mock
        mock_git = mock_git_cls.return_value
        mock_git.clone_or_pull.return_value = GitResult(success=True, output="cloned")

        # Instantiate manager again to use mock
        manager = WorkspaceManager(self.repo, self.main_work_dir)

        # Mock .git check
        with patch.object(Path, "exists", return_value=False):
            manager.ensure_main_repo()

        mock_git.clone_or_pull.assert_called_once()

    @patch("shared.workspace.GitOperations")
    def test_worktree_context_manager(self, mock_git_cls):
        """Test worktree creation and cleanup."""
        # Setup mock for main repo operations
        mock_main_git = MagicMock()
        mock_main_git.clone_or_pull.return_value = GitResult(success=True, output="")
        mock_main_git.worktree_add.return_value = GitResult(success=True, output="")
        mock_main_git.worktree_remove.return_value = GitResult(success=True, output="")
        mock_main_git.worktree_prune.return_value = GitResult(success=True, output="")
        mock_main_git.fetch_origin.return_value = GitResult(success=True, output="")
        mock_main_git.ensure_branch_up_to_date.return_value = GitResult(
            success=True, output=""
        )

        # Setup mock for worktree operations
        mock_worktree_git = MagicMock()

        # Configure GitOperations constructor to return different mocks
        # First call is main repo, second call is worktree
        mock_git_cls.side_effect = [mock_main_git, mock_worktree_git]

        manager = WorkspaceManager(self.repo, self.main_work_dir)

        # Mock ensure_main_repo checks
        with patch.object(Path, "exists", return_value=True):
            with manager.worktree("feature-branch", "agent-1") as wt_git:
                assert wt_git == mock_worktree_git

        # Verify fetch called once, then ensure + add
        mock_main_git.fetch_origin.assert_called_once()
        mock_main_git.worktree_add.assert_called_once()
        args = mock_main_git.worktree_add.call_args
        assert "agent-1" in str(args[0][0])
        assert args[0][1] == "feature-branch"

        mock_main_git.ensure_branch_up_to_date.assert_called_once_with("main")

        # Verify remove was called on exit
        mock_main_git.worktree_remove.assert_called()
        mock_main_git.worktree_prune.assert_called()

    @patch("shared.workspace.GitOperations")
    def test_worktree_context_manager_retries_on_failure(self, mock_git_cls):
        """Test retry logic when worktree add fails initially."""
        mock_main_git = MagicMock()
        mock_main_git.clone_or_pull.return_value = GitResult(success=True, output="")
        mock_main_git.worktree_remove.return_value = GitResult(success=True, output="")
        mock_main_git.worktree_prune.return_value = GitResult(success=True, output="")
        mock_main_git.worktree_add.side_effect = [
            GitResult(success=False, output="", error="exists"),
            GitResult(success=True, output="ok"),
        ]
        mock_main_git.fetch_origin.return_value = GitResult(success=True, output="")
        mock_main_git.ensure_branch_up_to_date.return_value = GitResult(
            success=True, output=""
        )

        mock_worktree_git = MagicMock()
        mock_git_cls.side_effect = [mock_main_git, mock_worktree_git]

        manager = WorkspaceManager(self.repo, self.main_work_dir)

        with patch.object(Path, "exists", return_value=True):
            with manager.worktree("fp", "agent-retry") as wt:
                assert wt == mock_worktree_git

        # fetch is called once before any worktree attempts
        mock_main_git.fetch_origin.assert_called_once()
        assert mock_main_git.worktree_add.call_count == 2
        assert mock_main_git.worktree_prune.call_count == 2
        # ensure_branch_up_to_date is called each time add_wrapped runs
        assert mock_main_git.ensure_branch_up_to_date.call_count == 2
