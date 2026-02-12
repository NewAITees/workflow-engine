"""Git operations for worker agent."""

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from shared.config import validate_dry_run_mode

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
        dry_run: str | None = None,
    ):
        """
        Initialize git operations.

        Args:
            repo: Repository in owner/repo format
            work_base: Base directory for workspaces (auto-generates workspace path)
            workspace_path: Explicit path to workspace (overrides work_base)
        """
        self.repo = repo
        self.dry_run = validate_dry_run_mode(dry_run)

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

    def _format_cmd(self, args: list[str]) -> str:
        """Format command for logs."""
        cmd = ["git"] + args
        return " ".join(shlex.quote(part) for part in cmd)

    def _dry_run_write(
        self, args: list[str], cwd: Path | None = None
    ) -> GitResult:
        """Log suppressed write operation."""
        work_dir = cwd or self.workspace
        logger.info(f"[DRY-RUN] would run: {self._format_cmd(args)}")
        return GitResult(
            success=True,
            output=f"[DRY-RUN] simulated in {work_dir}",
        )

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
            if self.dry_run:
                self._dry_run_write(["fetch", "origin"])
                self._dry_run_write(["checkout", "main"])
                self._dry_run_write(["reset", "--hard", "origin/main"])
                self._dry_run_write(["clean", "-fd"])
                return self._dry_run_write(["pull"])
            # Detect and checkout the default branch
            default_branch = self.get_default_branch()
            self._run(["fetch", "origin"], check=False)
            self._run(["checkout", default_branch], check=False)
            self._run(["reset", "--hard", f"origin/{default_branch}"])
            self._run(["clean", "-fd"])
            return self._run(["pull"])
        else:
            logger.info(f"Cloning repository to: {self.workspace}")
            if self.dry_run:
                return self._dry_run_write(
                    ["clone", f"https://github.com/{self.repo}.git", str(self.workspace)],
                    cwd=self.work_base,
                )
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
        if self.dry_run:
            self._dry_run_write(["checkout", "main"])
            self._dry_run_write(["pull"])
            self._dry_run_write(["branch", "-D", branch_name])
            return self._dry_run_write(["checkout", "-b", branch_name])

        default_branch = self.get_default_branch()

        # Ensure we're on the default branch first
        self._run(["checkout", default_branch])
        self._run(["pull"])

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
        if self.dry_run:
            self._dry_run_write(["fetch", "origin"])
            self._dry_run_write(["rev-parse", "--verify", f"origin/{branch_name}"])
            return self._dry_run_write(
                ["checkout", "-B", branch_name, f"origin/{branch_name}"]
            )

        self._run(["fetch", "origin"], check=False)
        remote_ref = f"origin/{branch_name}"
        remote_exists = self._run(["rev-parse", "--verify", remote_ref], check=False)

        if remote_exists.success:
            # Re-anchor local branch to the PR head tip to avoid stale/main divergence.
            return self._run(["checkout", "-B", branch_name, remote_ref])

        return self.create_branch(branch_name)

    def stage_all(self) -> GitResult:
        """Stage all changes."""
        if self.dry_run:
            return self._dry_run_write(["add", "-A"])
        return self._run(["add", "-A"])

    def commit(self, message: str) -> GitResult:
        """Create a commit."""
        if self.dry_run:
            self._dry_run_write(["add", "-A"])
            return self._dry_run_write(["commit", "-m", message])

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
        if self.dry_run:
            return self._dry_run_write(args)
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
        if self.dry_run:
            self._dry_run_write(["checkout", "main"])
            self._dry_run_write(["branch", "-D", branch_name])
            self._dry_run_write(["push", "origin", "--delete", branch_name])
            return
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
            if self.dry_run:
                return self._dry_run_write(
                    ["worktree", "add", "-b", branch, str(path), base_branch]
                )
            return self._run(["worktree", "add", "-b", branch, str(path), base_branch])
        # git worktree add <path> <branch>
        if self.dry_run:
            return self._dry_run_write(["worktree", "add", str(path), branch])
        return self._run(["worktree", "add", str(path), branch])

    def worktree_remove(self, path: Path) -> GitResult:
        """Remove a worktree."""
        # git worktree remove <path>
        if self.dry_run:
            return self._dry_run_write(["worktree", "remove", str(path)])
        return self._run(["worktree", "remove", str(path)])

    def worktree_prune(self) -> GitResult:
        """Prune worktree information."""
        if self.dry_run:
            return self._dry_run_write(["worktree", "prune"])
        return self._run(["worktree", "prune"])

    @property
    def path(self) -> Path:
        """Get the workspace path."""
        return self.workspace
