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

    def __init__(self, repo: str, work_base: Path):
        """
        Initialize git operations.

        Args:
            repo: Repository in owner/repo format
            work_base: Base directory for workspaces
        """
        self.repo = repo
        self.work_base = Path(work_base)
        self.work_base.mkdir(parents=True, exist_ok=True)

        # Workspace for this repo
        self.workspace = self.work_base / repo.replace("/", "_")

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
            # Detect and checkout the default branch
            default_branch = self.get_default_branch()
            self._run(["fetch", "origin"], check=False)
            self._run(["checkout", default_branch], check=False)
            self._run(["reset", "--hard", f"origin/{default_branch}"])
            self._run(["clean", "-fd"])
            return self._run(["pull"])
        else:
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

        # Ensure we're on the default branch first
        self._run(["checkout", default_branch])
        self._run(["pull"])

        # Delete branch if it exists locally
        self._run(["branch", "-D", branch_name], check=False)

        # Create new branch
        return self._run(["checkout", "-b", branch_name])

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
            args.insert(1, "--force")
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

    @property
    def path(self) -> Path:
        """Get the workspace path."""
        return self.workspace
