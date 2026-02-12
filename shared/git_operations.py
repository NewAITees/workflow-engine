"""Git operations for worker agent."""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class GitResult:
    """Result of a git operation."""

    success: bool
    output: str
    error: str | None = None


class GitOperations:
    """Git operations for implementing features."""

    def __init__(
        self,
        repo: str,
        work_base: Path | None = None,
        workspace_path: Path | None = None,
    ):
        """
        Initialize git operations.

        Args:
            repo: Repository in owner/repo format
            work_base: Base directory for workspaces (auto-generates workspace path)
            workspace_path: Explicit path to workspace (overrides work_base)
        """
        self.repo = repo

        if workspace_path:
            self.workspace = Path(workspace_path)
            # If explicit path is given, work_base might not be set or relevant,
            # but we assume the parent is the base if needed.
            self.work_base = self.workspace.parent
        elif work_base:
            self.work_base = Path(work_base)
            self.work_base.mkdir(parents=True, exist_ok=True)
            self.workspace = self.work_base / repo.replace("/", "_")
        else:
            raise ValueError("Either work_base or workspace_path must be provided")

    def _run(
        self,
        args: list[str],
        cwd: Path | None = None,
        check: bool = True,
    ) -> GitResult:
        """Run a git command."""
        cmd = ["git"] + args
        work_dir = cwd or self.workspace

        logger.debug(f"Running: {' '.join(cmd)} in {work_dir}")

        try:
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                check=check,
            )
            # When check=False, we need to manually check returncode
            if result.returncode != 0:
                return GitResult(
                    success=False,
                    output=result.stdout,
                    error=result.stderr or f"Exit code: {result.returncode}",
                )
            return GitResult(success=True, output=result.stdout)
        except subprocess.CalledProcessError as e:
            return GitResult(success=False, output=e.stdout or "", error=e.stderr)

    def clone_or_pull(self) -> GitResult:
        """Clone the repository or pull if it exists."""
        if self.workspace.exists():
            logger.info(f"Updating existing workspace: {self.workspace}")
            default_branch = self.get_default_branch()
            return self.ensure_branch_up_to_date(default_branch)
        logger.info(f"Cloning repository to: {self.workspace}")
        return self._run(
            ["clone", f"https://github.com/{self.repo}.git", str(self.workspace)],
            cwd=self.work_base,
        )

    def get_default_branch(self) -> str:
        """Get the default branch name (main or master)."""
        result = self._run(["symbolic-ref", "refs/remotes/origin/HEAD"], check=False)
        if result.success:
            # Output like: refs/remotes/origin/main
            return result.output.strip().split("/")[-1]
        # Fallback
        return "main"

    def create_branch(self, branch_name: str) -> GitResult:
        """Create and checkout a new branch."""
        default_branch = self.get_default_branch()

        ensure_result = self.ensure_branch_up_to_date(default_branch)
        if not ensure_result.success:
            return ensure_result

        # Delete branch if it exists locally
        self._run(["branch", "-D", branch_name], check=False)

        # Create new branch
        return self._run(["checkout", "-b", branch_name])

    def checkout_branch_from_remote(self, branch_name: str) -> GitResult:
        """
        Checkout a local branch from origin/<branch_name> if it exists.

        Falls back to creating a fresh branch from default branch when
        the remote branch does not exist.
        """
        remote_ref = f"origin/{branch_name}"
        remote_exists = self._run(["rev-parse", "--verify", remote_ref], check=False)

        if remote_exists.success:
            return self.ensure_branch_up_to_date(branch_name)

        return self.create_branch(branch_name)

    def stage_all(self) -> GitResult:
        """Stage all changes."""
        return self._run(["add", "-A"])

    def commit(self, message: str) -> GitResult:
        """Create a commit."""
        # Stage all changes first
        self.stage_all()

        # Check if there are changes to commit
        status = self._run(["status", "--porcelain"])
        if not status.output.strip():
            return GitResult(success=False, output="", error="No changes to commit")

        return self._run(["commit", "-m", message])

    def push(self, branch_name: str, force: bool = False) -> GitResult:
        """Push branch to origin."""
        args = ["push", "-u", "origin", branch_name]
        if force:
            args.insert(1, "--force-with-lease")
        return self._run(args)

    def get_diff(self, base_branch: str | None = None) -> str:
        """Get diff against base branch."""
        if base_branch is None:
            base_branch = self.get_default_branch()

        result = self._run(["diff", base_branch])
        return result.output if result.success else ""

    def get_status(self) -> str:
        """Get git status."""
        result = self._run(["status"])
        return result.output if result.success else ""

    def cleanup_branch(self, branch_name: str) -> None:
        """Clean up a branch (local and remote)."""
        default_branch = self.get_default_branch()
        self._run(["checkout", default_branch], check=False)
        self._run(["branch", "-D", branch_name], check=False)
        self._run(["push", "origin", "--delete", branch_name], check=False)

    def worktree_add(
        self,
        path: Path,
        branch: str,
        *,
        create_branch: bool = False,
        base_branch: str = "main",
    ) -> GitResult:
        """Add a new worktree."""
        # Ensure path parent exists
        path.parent.mkdir(parents=True, exist_ok=True)
        if create_branch:
            # git worktree add -b <branch> <path> <base_branch>
            return self._run(["worktree", "add", "-b", branch, str(path), base_branch])
        # git worktree add <path> <branch>
        return self._run(["worktree", "add", str(path), branch])

    def ensure_branch_up_to_date(self, branch: str) -> GitResult:
        """Ensure the local branch tracks and matches origin/<branch>."""
        fetch_result = self._run(["fetch", "origin"], check=False)
        if not fetch_result.success:
            return fetch_result

        checkout_result = self._run(["checkout", branch], check=False)
        if not checkout_result.success:
            remote_ref = f"origin/{branch}"
            checkout_result = self._run(
                ["checkout", "-B", branch, remote_ref], check=False
            )
            if not checkout_result.success:
                return checkout_result

        reset_result = self._run(
            [
                "reset",
                "--hard",
                f"origin/{branch}",
            ]
        )
        if not reset_result.success:
            return reset_result

        clean_result = self._run(["clean", "-fd"])
        return clean_result

    def worktree_remove(self, path: Path) -> GitResult:
        """Remove a worktree."""
        # git worktree remove <path>
        return self._run(["worktree", "remove", str(path)])

    def worktree_prune(self) -> GitResult:
        """Prune worktree information."""
        return self._run(["worktree", "prune"])

    @property
    def path(self) -> Path:
        """Get the workspace path."""
        return self.workspace
