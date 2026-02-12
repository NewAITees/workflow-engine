#!/usr/bin/env python3
"""Worker Agent - Autonomous implementation daemon.

Watches for issues with 'status:ready' label and automatically
implements them using the configured LLM backend (codex or claude).
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.action_pack import (
    ActionPack,
    build_action_pack,
    classify_commit_result,
    parse_mypy_output,
    parse_pytest_output,
    parse_ruff_output,
)
from shared.config import get_agent_config
from shared.git_operations import GitOperations
from shared.github_client import GitHubClient, Issue, PullRequest
from shared.llm_client import LLMClient
from shared.lock import LockManager
from shared.workspace import WorkspaceManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("worker-agent")


class WorkerAgent:
    """Autonomous worker that implements issues."""

    # Status labels
    STATUS_READY = "status:ready"
    STATUS_IMPLEMENTING = "status:implementing"
    STATUS_TESTING = "status:testing"
    STATUS_REVIEWING = "status:reviewing"
    STATUS_CHANGES_REQUESTED = "status:changes-requested"
    STATUS_FAILED = "status:failed"
    STATUS_CI_FAILED = "status:ci-failed"
    STATUS_NEEDS_CLARIFICATION = "status:needs-clarification"
    RETRY_MARKER = "WORKER_RETRY"

    # Retry settings
    MAX_RETRIES = 3
    MAX_CI_RETRIES = 3

    # CI settings
    CI_WAIT_TIMEOUT = 600  # 10 minutes
    CI_CHECK_INTERVAL = 30  # 30 seconds

    MIN_SPEC_LENGTH = 100
    SPEC_UNCLEAR_KEYWORDS = [
        "ambiguous",
        "unclear",
        "not specified",
        "undefined behavior",
        "missing requirement",
        "conflicting requirement",
        "cannot determine",
        "insufficient information",
    ]

    def __init__(self, repo: str, config_path: str | None = None):
        self.repo = repo
        self.config = get_agent_config(repo, config_path)
        assert self.config.work_dir is not None

        # Generate unique agent ID
        self.agent_id = f"worker-{uuid.uuid4().hex[:8]}"

        # Initialize components
        self.github = GitHubClient(repo, gh_cli=self.config.gh_cli)
        self.lock = LockManager(
            self.github, agent_type="worker", agent_id=self.agent_id
        )
        self.llm = LLMClient(self.config)
        self.git = GitOperations(repo, Path(self.config.work_dir))
        self.workspace_manager = WorkspaceManager(repo, self.git.path)
        self._latest_quality_action_pack: ActionPack | None = None
        self._latest_test_action_pack: ActionPack | None = None
        self._latest_commit_action_pack: ActionPack | None = None

        logger.info(f"Worker Agent initialized for {repo}")
        logger.info(f"Agent ID: {self.agent_id}")
        logger.info(f"LLM backend: {self.config.llm_backend}")
        logger.info(f"Work directory: {self.git.workspace}")

    def run(self) -> None:
        """Main daemon loop."""
        logger.info(f"Starting Worker Agent daemon for {self.repo}")
        logger.info(f"Poll interval: {self.config.poll_interval}s")

        while True:
            try:
                self._process_stale_locks()
                self._process_ready_issues()
                self._process_changes_requested_prs()
                time.sleep(self.config.poll_interval)

            except KeyboardInterrupt:
                logger.info("Shutting down Worker Agent")
                break
            except Exception as e:
                logger.exception(f"Unexpected error: {e}")
                time.sleep(60)  # Wait before retry

    def run_once(self) -> bool:
        """Process one issue/PR and return. For testing."""
        if self._process_stale_locks():
            return True

        # Try ready issues first
        issues = self.github.list_issues(labels=[self.STATUS_READY])
        if issues:
            return self._try_process_issue(issues[0])

        # Then try changes-requested PRs
        prs = self.github.list_prs(labels=[self.STATUS_CHANGES_REQUESTED])
        if prs:
            return self._try_retry_pr(prs[0])

        logger.info("No work to process")
        return False

    def _get_stale_timeout_minutes(self) -> int:
        """Get stale lock timeout from config with safe fallback."""
        value = getattr(self.config, "stale_lock_timeout_minutes", 30)
        try:
            timeout = int(value)
        except (TypeError, ValueError):
            timeout = 30
        return timeout if timeout > 0 else 30

    def _parse_github_datetime(self, ts: str | None) -> datetime | None:
        """Parse GitHub timestamp string into timezone-aware datetime."""
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _get_lock_reference_time(self, number: int) -> datetime | None:
        """
        Get reference timestamp for lock staleness.

        Priority:
        1. Most recent worker ACK comment timestamp
        2. Most recent comment timestamp
        """
        comments = self.github.get_issue_comments(number, limit=50)
        latest_comment_time: datetime | None = None
        latest_ack_time: datetime | None = None

        for comment in comments:
            created = self._parse_github_datetime(comment.get("created_at"))
            if created is None:
                continue

            if latest_comment_time is None or created > latest_comment_time:
                latest_comment_time = created

            body = str(comment.get("body", ""))
            if body.startswith("ACK:worker:"):
                if latest_ack_time is None or created > latest_ack_time:
                    latest_ack_time = created

        return latest_ack_time or latest_comment_time

    def _is_stale_lock(self, number: int) -> tuple[bool, float | None]:
        """Check if implementing lock is stale based on reference timestamp."""
        ref_time = self._get_lock_reference_time(number)
        if ref_time is None:
            return False, None

        age_minutes = (datetime.now(UTC) - ref_time).total_seconds() / 60.0
        return age_minutes > self._get_stale_timeout_minutes(), age_minutes

    def _get_pr_recovery_status(self, pr_number: int) -> str:
        """
        Determine status to restore after stale recovery.

        If a CHANGES_REQUESTED review exists, restore to changes-requested.
        Otherwise restore to reviewing.
        """
        reviews = self.github.get_pr_reviews(pr_number)
        for review in reversed(reviews):
            if review.get("state") == "CHANGES_REQUESTED":
                return self.STATUS_CHANGES_REQUESTED
        return self.STATUS_REVIEWING

    def _process_stale_locks(self) -> bool:
        """Recover stale locks from issues/PRs stuck in implementing."""
        recovered = False
        timeout = self._get_stale_timeout_minutes()

        issues = self.github.list_issues(labels=[self.STATUS_IMPLEMENTING])
        for issue in issues:
            is_stale, age_minutes = self._is_stale_lock(issue.number)
            if not is_stale:
                continue

            age_text = (
                f"{age_minutes:.1f} minutes" if age_minutes is not None else "unknown"
            )
            logger.warning(
                f"[{self.agent_id}] Recovering stale issue lock #{issue.number} "
                f"(age={age_text}, timeout={timeout}m)"
            )
            self.github.remove_label(issue.number, self.STATUS_IMPLEMENTING)
            self.github.add_label(issue.number, self.STATUS_READY)
            self.github.comment_issue(
                issue.number,
                f"⚠️ **Recovered stale lock**\n\n"
                f"- Previous status: `{self.STATUS_IMPLEMENTING}`\n"
                f"- New status: `{self.STATUS_READY}`\n"
                f"- Lock age: {age_text}\n"
                f"- Timeout: {timeout} minutes",
            )
            recovered = True

        prs = self.github.list_prs(labels=[self.STATUS_IMPLEMENTING])
        for pr in prs:
            is_stale, age_minutes = self._is_stale_lock(pr.number)
            if not is_stale:
                continue

            target_status = self._get_pr_recovery_status(pr.number)
            age_text = (
                f"{age_minutes:.1f} minutes" if age_minutes is not None else "unknown"
            )
            logger.warning(
                f"[{self.agent_id}] Recovering stale PR lock #{pr.number} "
                f"(age={age_text}, timeout={timeout}m) -> {target_status}"
            )
            self.github.remove_pr_label(pr.number, self.STATUS_IMPLEMENTING)
            self.github.add_pr_label(pr.number, target_status)
            self.github.comment_pr(
                pr.number,
                f"⚠️ **Recovered stale lock**\n\n"
                f"- Previous status: `{self.STATUS_IMPLEMENTING}`\n"
                f"- New status: `{target_status}`\n"
                f"- Lock age: {age_text}\n"
                f"- Timeout: {timeout} minutes",
            )
            recovered = True

        return recovered

    def _process_ready_issues(self) -> None:
        """Find and process ready issues."""
        issues = self.github.list_issues(labels=[self.STATUS_READY])

        if not issues:
            logger.debug("No ready issues found")
            return

        logger.info(f"Found {len(issues)} ready issue(s)")

        for issue in issues:
            if self._try_process_issue(issue):
                logger.info(f"Successfully processed issue #{issue.number}")
            else:
                logger.debug(f"Skipped issue #{issue.number}")

    @contextmanager
    def _issue_workspace(
        self, issue_number: int, branch_name: str
    ) -> Generator[GitOperations, None, None]:
        """Provide a per-issue worktree and fall back to legacy workspace if needed."""
        if not isinstance(self.git, GitOperations):
            clone_result = self.git.clone_or_pull()
            if not clone_result.success:
                raise RuntimeError(f"Failed to prepare workspace: {clone_result.error}")

            branch_result = self.git.create_branch(branch_name)
            if not branch_result.success:
                raise RuntimeError(f"Failed to create branch: {branch_result.error}")

            yield self.git
            return

        default_branch = self.github.get_default_branch()
        worktree_id = f"{self.agent_id}-issue-{issue_number}"
        worktree_cm = None
        work_git = None

        try:
            worktree_cm = self.workspace_manager.worktree(
                branch_name,
                worktree_id,
                create_branch=True,
                base_branch=default_branch,
            )
            work_git = worktree_cm.__enter__()
        except Exception as e:
            # Branch may already exist from a previous attempt.
            if "already exists" in str(e):
                logger.warning(
                    f"[{self.agent_id}] Worktree branch already exists for issue #{issue_number}; "
                    "retrying by reusing existing branch."
                )
                try:
                    worktree_cm = self.workspace_manager.worktree(
                        branch_name,
                        worktree_id,
                        create_branch=False,
                        base_branch=default_branch,
                    )
                    work_git = worktree_cm.__enter__()
                except Exception as retry_error:
                    e = retry_error

            if work_git is None:
                logger.warning(
                    f"[{self.agent_id}] Worktree setup failed for issue #{issue_number}: {e}. "
                    "Falling back to legacy workspace flow."
                )

        if work_git is not None and worktree_cm is not None:
            exc_type = exc_value = exc_tb = None
            try:
                yield work_git
                return
            except Exception:
                exc_type, exc_value, exc_tb = sys.exc_info()
                raise
            finally:
                worktree_cm.__exit__(exc_type, exc_value, exc_tb)

        clone_result = self.git.clone_or_pull()
        if not clone_result.success:
            raise RuntimeError(f"Failed to prepare workspace: {clone_result.error}")

        branch_result = self.git.create_branch(branch_name)
        if not branch_result.success:
            raise RuntimeError(f"Failed to create branch: {branch_result.error}")

        yield self.git

    def _try_process_issue(self, issue: Issue) -> bool:
        """
        Try to process a single issue with TDD flow.

        TDD Flow:
        1. Generate tests first
        2. Generate implementation
        3. Run tests
        4. Retry on failure (max 3 times)

        Returns True if processed successfully, False otherwise.
        """
        logger.info(
            f"[{self.agent_id}] Attempting to process issue #{issue.number}: {issue.title}"
        )

        test_retry_count = 0

        # Try to acquire lock
        lock_result = self.lock.try_lock_issue(
            issue.number,
            self.STATUS_READY,
            self.STATUS_IMPLEMENTING,
        )

        if not lock_result.success:
            logger.debug(f"Could not lock issue #{issue.number}: {lock_result.error}")
            return False

        branch_name = f"auto/issue-{issue.number}"

        try:
            with self._issue_workspace(issue.number, branch_name) as issue_git:
                before_test_files = self._snapshot_test_files(issue_git.path)

                # TDD Step 1: Generate tests first
                logger.info(
                    f"[{self.agent_id}] Generating tests with {self.config.llm_backend}..."
                )
                test_result = self.llm.generate_tests(
                    spec=(
                        f"{issue.body}\n\n"
                        "## Test File Requirement\n"
                        f"Create/overwrite exactly one issue test file at "
                        f"`tests/test_issue_{issue.number}.py`."
                    ),
                    repo_context=(
                        f"Repository: {self.repo}\n"
                        f"Required test path: tests/test_issue_{issue.number}.py"
                    ),
                    work_dir=issue_git.path,
                )

                if not test_result.success:
                    raise RuntimeError(f"Test generation failed: {test_result.error}")

                self._ensure_issue_test_file(
                    issue.number, issue_git.path, before_test_files
                )

                # Commit tests
                logger.info(f"[{self.agent_id}] Committing tests...")
                test_commit_msg = (
                    f"test: add tests for #{issue.number}\n\n{issue.title}"
                )
                test_commit_result = issue_git.commit(test_commit_msg)

                if not test_commit_result.success:
                    raise RuntimeError(
                        f"Test commit failed: {test_commit_result.error}"
                    )

                # TDD Step 2-4: Implementation with test retry loop
                validation_failure_output = ""
                test_passed = False

                while test_retry_count < self.MAX_RETRIES:
                    logger.info(
                        f"[{self.agent_id}] Generating implementation "
                        f"(attempt {test_retry_count + 1}/{self.MAX_RETRIES})..."
                    )

                    # Prepare spec with test feedback if retrying
                    impl_spec = issue.body
                    if test_retry_count > 0:
                        impl_spec += (
                            f"\n\n## Previous Validation Failure (Attempt {test_retry_count})\n"
                            "Please fix the implementation to satisfy quality checks and tests.\n\n"
                            f"```\n{validation_failure_output[:2000]}\n```"
                        )

                    # Generate implementation
                    gen_result = self.llm.generate_implementation(
                        spec=impl_spec,
                        repo_context=f"Repository: {self.repo}\nTDD attempt: {test_retry_count + 1}/{self.MAX_RETRIES}",
                        work_dir=issue_git.path,
                    )

                    if not gen_result.success:
                        raise RuntimeError(f"Implementation failed: {gen_result.error}")

                    # Commit implementation
                    logger.info(f"[{self.agent_id}] Committing implementation...")
                    impl_commit_msg = f"feat: implement #{issue.number} (attempt {test_retry_count + 1})\n\n{issue.title}"
                    impl_commit_result = issue_git.commit(impl_commit_msg)

                    commit_status = classify_commit_result(
                        impl_commit_result.success,
                        impl_commit_result.output,
                        impl_commit_result.error or "",
                    )
                    if commit_status == "failed":
                        raise RuntimeError(
                            f"Implementation commit failed: {impl_commit_result.error}"
                        )
                    if commit_status == "no_op":
                        commit_check = {
                            "name": "commit",
                            "exit_code": 1,
                            "result": "no_op",
                            "error_type": "commit_no_changes",
                            "primary_message": "No changes to commit",
                            "evidence": "No changes to commit",
                        }
                        self._latest_commit_action_pack = build_action_pack(
                            task=self._build_task_context(
                                issue.number, attempt=test_retry_count + 1
                            ),
                            phase="finalize",
                            status="no_op",
                            checks=[commit_check],
                            blockers=[],
                        )
                        self.github.comment_issue(
                            issue.number,
                            "ℹ️ **Implementation commit resulted in no-op**\n\n"
                            f"{self._render_action_pack_comment(self._latest_commit_action_pack)}",
                        )
                        self.github.remove_label(issue.number, self.STATUS_IMPLEMENTING)
                        self.github.add_label(issue.number, self.STATUS_READY)
                        return False

                    # Run quality gate before issue-specific tests
                    logger.info(
                        f"[{self.agent_id}] Running quality checks (ruff/mypy)..."
                    )
                    quality_passed, quality_output = self._run_quality_checks(
                        git_path=issue_git.path,
                        issue_number=issue.number,
                    )
                    if not quality_passed:
                        test_retry_count += 1
                        validation_failure_output = quality_output
                        logger.warning(
                            f"[{self.agent_id}] Quality checks failed (attempt {test_retry_count}/{self.MAX_RETRIES})"
                        )
                        quality_pack = self._latest_quality_action_pack
                        quality_body = (
                            self._render_action_pack_comment(quality_pack)
                            if quality_pack is not None
                            else quality_output[:2000]
                        )
                        self.github.comment_issue(
                            issue.number,
                            f"⚠️ **Quality checks failed (attempt {test_retry_count}/{self.MAX_RETRIES})**\n\n"
                            "Retrying implementation with quality feedback...\n\n"
                            f"{quality_body}",
                        )
                        if test_retry_count >= self.MAX_RETRIES:
                            raise RuntimeError(
                                f"Quality checks failed after {self.MAX_RETRIES} attempts:\n{quality_output[:500]}"
                            )
                        continue

                    # Transition to testing status
                    self.github.remove_label(issue.number, self.STATUS_IMPLEMENTING)
                    self.github.add_label(issue.number, self.STATUS_TESTING)

                    # Run tests
                    logger.info(f"[{self.agent_id}] Running tests...")
                    test_passed, test_output = self._run_tests(
                        issue.number, git_path=issue_git.path
                    )

                    if test_passed:
                        logger.info(
                            f"[{self.agent_id}] Tests passed on attempt {test_retry_count + 1}"
                        )
                        break

                    # Tests failed - record and retry
                    test_retry_count += 1
                    validation_failure_output = test_output

                    logger.warning(
                        f"[{self.agent_id}] Tests failed (attempt {test_retry_count}/{self.MAX_RETRIES})"
                    )

                    test_pack = self._latest_test_action_pack
                    test_body = (
                        self._render_action_pack_comment(test_pack)
                        if test_pack is not None
                        else test_output[:2000]
                    )
                    # Comment on issue about test failure
                    self.github.comment_issue(
                        issue.number,
                        f"⚠️ **Test failed (attempt {test_retry_count}/{self.MAX_RETRIES})**\n\n"
                        f"Retrying implementation with test feedback...\n\n"
                        f"{test_body}",
                    )

                    if test_retry_count >= self.MAX_RETRIES:
                        # Max retries exceeded
                        raise RuntimeError(
                            f"Tests failed after {self.MAX_RETRIES} attempts:\n{test_output[:500]}"
                        )

                    # Transition back to implementing for retry
                    self.github.remove_label(issue.number, self.STATUS_TESTING)
                    self.github.add_label(issue.number, self.STATUS_IMPLEMENTING)

                # Tests passed! Transition to reviewing
                self.github.remove_label(issue.number, self.STATUS_TESTING)

                # Push branch
                logger.info(f"[{self.agent_id}] Pushing to remote...")
                push_result = issue_git.push(branch_name, force=True)
                if not push_result.success:
                    raise RuntimeError(f"Push failed: {push_result.error}")

                # Create pull request
                logger.info(f"[{self.agent_id}] Creating pull request...")
                pr_body = f"""## Summary
Auto-generated implementation for #{issue.number} using TDD approach.

## Original Issue
{issue.title}

## Implementation
Generated by Worker Agent ({self.agent_id}) using {self.config.llm_backend}.

**TDD Process:**
- ✅ Tests generated first
- ✅ Implementation created
- ✅ All tests passed (attempts: {test_retry_count + 1}/{self.MAX_RETRIES})

Closes #{issue.number}

---
🤖 Auto-generated by Workflow Engine Worker Agent
"""

                # Get default branch for PR base
                default_branch = self.github.get_default_branch()

                pr_url = self.github.create_pr(
                    title=f"Auto: {issue.title}",
                    body=pr_body,
                    head=branch_name,
                    base=default_branch,
                    labels=[self.STATUS_REVIEWING],
                )

                if not pr_url:
                    raise RuntimeError("Failed to create pull request")

                logger.info(f"[{self.agent_id}] Pull request created: {pr_url}")

                # Extract PR number from URL
                pr_number = int(pr_url.split("/")[-1])

                self.github.comment_pr(
                    pr_number,
                    f"🧪 **Local TDD validation passed**\n\n"
                    f"- Tests generated before implementation\n"
                    f"- Local test attempts: {test_retry_count + 1}/{self.MAX_RETRIES}\n"
                    f"- Waiting for CI verification",
                )

                # Wait for CI and handle failures with retry loop
                logger.info(f"[{self.agent_id}] Waiting for CI to complete...")
                ci_passed, ci_status = self._wait_for_ci(
                    pr_number, timeout=self.CI_WAIT_TIMEOUT
                )

                ci_retry_count = 0
                while (
                    not ci_passed
                    and ci_status == "failure"
                    and ci_retry_count < self.MAX_CI_RETRIES
                ):
                    ci_retry_count += 1
                    logger.warning(
                        f"[{self.agent_id}] CI failed (attempt {ci_retry_count}/{self.MAX_CI_RETRIES})"
                    )

                    # Get CI failure logs
                    ci_logs = self._get_ci_failure_logs(pr_number)

                    # Comment about CI failure
                    self.github.comment_pr(
                        pr_number,
                        f"⚠️ **CI failed (attempt {ci_retry_count}/{self.MAX_CI_RETRIES})**\n\n"
                        f"Analyzing failures and attempting automatic fix...\n\n"
                        f"<details>\n<summary>CI failure details</summary>\n\n"
                        f"{ci_logs[:2000]}\n\n</details>",
                    )

                    # Ask LLM to fix CI failures
                    fix_spec = f"""{issue.body}

## CI Failure (Attempt {ci_retry_count})
The implementation passed local tests but CI checks failed.
Please analyze and fix the CI failures.

{ci_logs}
"""

                    logger.info(
                        f"[{self.agent_id}] Asking LLM to fix CI failures (attempt {ci_retry_count})..."
                    )
                    fix_result = self.llm.generate_implementation(
                        spec=fix_spec,
                        repo_context=f"Repository: {self.repo}\nCI fix attempt: {ci_retry_count}/{self.MAX_CI_RETRIES}",
                        work_dir=issue_git.path,
                    )

                    if not fix_result.success:
                        logger.error(
                            f"[{self.agent_id}] CI fix generation failed: {fix_result.error}"
                        )
                        break

                    # Commit CI fix
                    fix_commit_msg = f"fix: address CI failures for #{issue.number} (attempt {ci_retry_count})\n\n{ci_logs[:200]}"
                    fix_commit_result = issue_git.commit(fix_commit_msg)

                    if not fix_commit_result.success:
                        logger.error(
                            f"[{self.agent_id}] CI fix commit failed: {fix_commit_result.error}"
                        )
                        break

                    # Push fix
                    push_result = issue_git.push(branch_name)
                    if not push_result.success:
                        logger.error(
                            f"[{self.agent_id}] CI fix push failed: {push_result.error}"
                        )
                        break

                    # Wait for CI again
                    logger.info(f"[{self.agent_id}] Waiting for CI after fix...")
                    ci_passed, ci_status = self._wait_for_ci(
                        pr_number, timeout=self.CI_WAIT_TIMEOUT
                    )

                # Check final CI status
                if not ci_passed:
                    if ci_status == "pending":
                        # CI still pending after timeout
                        logger.warning(
                            f"[{self.agent_id}] CI still pending after timeout, marking for manual review"
                        )
                        self.github.comment_pr(
                            pr_number,
                            "⏱️ **CI checks are taking longer than expected**\n\n"
                            "The PR is ready for manual review while CI completes.",
                        )
                    elif ci_status == "failure":
                        # CI failed after all retries
                        logger.error(
                            f"[{self.agent_id}] CI failed after {ci_retry_count} fix attempts"
                        )

                        # Mark PR for changes requested due to failed tests/checks
                        self.github.add_pr_label(pr_number, self.STATUS_CI_FAILED)
                        self.github.add_pr_label(
                            pr_number, self.STATUS_CHANGES_REQUESTED
                        )
                        self.github.remove_pr_label(pr_number, self.STATUS_REVIEWING)

                        self.github.comment_pr(
                            pr_number,
                            f"❌ **CI checks failed after {ci_retry_count} automatic fix attempts**\n\n"
                            f"Manual intervention required. Please review the CI logs and fix the issues.\n\n"
                            f"The PR has been marked with `{self.STATUS_CI_FAILED}` label.",
                        )

                        # Also comment on issue
                        self.github.comment_issue(
                            issue.number,
                            f"⚠️ **CI checks failed**\n\n"
                            f"Pull Request: {pr_url}\n\n"
                            f"Attempted {ci_retry_count} automatic fixes but CI still failing.\n"
                            f"Manual review needed.",
                        )

                        return False
                else:
                    logger.info(f"[{self.agent_id}] CI passed!")
                    if ci_retry_count > 0:
                        self.github.comment_pr(
                            pr_number,
                            f"✅ **CI now passing after {ci_retry_count} automatic fix(es)!**",
                        )

                # Comment on issue about successful completion
                ci_info = ""
                if ci_retry_count > 0:
                    ci_info = (
                        f"\n- CI fixes: {ci_retry_count} automatic fix(es) applied"
                    )

                self.github.comment_issue(
                    issue.number,
                    f"✅ **Implementation complete with TDD!**\n\n"
                    f"- Tests generated and passed\n"
                    f"- Test attempts: {test_retry_count + 1}/{self.MAX_RETRIES}{ci_info}\n\n"
                    f"Pull Request: {pr_url}",
                )

                return True

        except Exception as e:
            logger.error(
                f"[{self.agent_id}] Failed to process issue #{issue.number}: {e}"
            )

            failure_reason = str(e)
            if "Tests failed after" in failure_reason:
                self._comment_worker_escalation(
                    issue.number,
                    "test-retry-limit",
                    "Test fix loop exhausted maximum retries. Please review test design and specification.",
                )
                self.github.remove_label(issue.number, self.STATUS_IMPLEMENTING)
                self.github.add_label(issue.number, self.STATUS_NEEDS_CLARIFICATION)
            elif self._is_specification_unclear(failure_reason, issue.body):
                self.lock.mark_needs_clarification(
                    issue.number,
                    self.STATUS_IMPLEMENTING,
                    failure_reason,
                )

                feedback = self._generate_planner_feedback(
                    issue_number=issue.number,
                    spec=issue.body,
                    failure_reason=failure_reason,
                    attempt_count=test_retry_count + 1,
                )

                self.github.comment_issue(
                    issue.number,
                    f"⚠️ **Implementation failed - Specification clarification needed**\n\n"
                    f"{feedback}\n\n"
                    f"@Planner: Please review and clarify the specification.",
                )
                self._comment_worker_escalation(
                    issue.number,
                    "spec-clarification-needed",
                    "Specification is ambiguous or incomplete for implementation.",
                )

                self.github.remove_label(issue.number, self.STATUS_IMPLEMENTING)
                self.github.add_label(issue.number, self.STATUS_NEEDS_CLARIFICATION)
            else:
                # Mark as failed
                self.lock.mark_failed(
                    issue.number,
                    self.STATUS_IMPLEMENTING,
                    failure_reason,
                )

            # Cleanup branch if it was created
            self.git.cleanup_branch(f"auto/issue-{issue.number}")

            return False

    def _is_specification_unclear(self, failure_reason: str, spec: str) -> bool:
        """Determine if a failure indicates an unclear specification."""
        failure_lower = failure_reason.lower()

        if any(keyword in failure_lower for keyword in self.SPEC_UNCLEAR_KEYWORDS):
            return True

        if len(spec.strip()) < self.MIN_SPEC_LENGTH:
            return True

        if "test failed after" in failure_lower and "different" in failure_lower:
            return True

        return False

    def _generate_planner_feedback(
        self,
        issue_number: int,
        spec: str,
        failure_reason: str,
        attempt_count: int,
    ) -> str:
        """Generate detailed feedback for the Planner."""
        feedback = f"""## Implementation Failure Analysis

**Issue Number:** #{issue_number}
**Attempts Made:** {attempt_count}/{self.MAX_RETRIES}
**Agent ID:** {self.agent_id}

### Failure Reason
```
{failure_reason[:1000]}
```

### Original Specification
```
{spec[:1000]}
```

### Clarification Needed

Based on the failure analysis, the specification may need improvement in these areas:

1. **Acceptance Criteria**: Are the success conditions clearly defined?
2. **Edge Cases**: Are boundary conditions and error cases specified?
3. **Dependencies**: Are all required dependencies and prerequisites listed?
4. **Data Formats**: Are input/output formats clearly specified?
5. **Error Handling**: How should errors and exceptions be handled?

### Recommendations for Planner

Please review the specification and:
- Add missing acceptance criteria
- Clarify ambiguous requirements
- Specify edge case handling
- Add concrete examples
- Break down complex requirements into smaller steps

Once clarified, please update the issue and change label from `status:needs-clarification` back to `status:ready`.
"""

        return feedback

    def _comment_worker_escalation(
        self, issue_number: int, reason: str, details: str
    ) -> None:
        """Post a normalized worker escalation marker for Planner loop pickup."""
        self.github.comment_issue(
            issue_number,
            f"ESCALATION:worker\n\nReason: {reason}\n\n{details}",
        )

    def _build_task_context(self, issue_number: int, attempt: int = 1) -> dict:
        """Build Action Pack task context."""
        return {
            "repo": self.repo,
            "issue_number": issue_number,
            "attempt": attempt,
            "agent": self.agent_id,
        }

    def _render_action_pack_comment(self, pack: ActionPack) -> str:
        """Render compact Action Pack comment body."""
        lines = [f"Summary: {pack['summary']}"]

        evidence_lines = []
        for check in pack.get("checks", []):
            if check.get("result") == "ok":
                continue
            evidence = str(check.get("evidence", "")).strip()
            if evidence:
                evidence_lines.append(
                    f"- `{check.get('name', 'check')}`: {evidence[:300]}"
                )
        if evidence_lines:
            lines.append("\nEvidence:")
            lines.extend(evidence_lines[:2])

        action_lines = []
        for action in pack.get("actions", []):
            action_lines.append(
                f"- {action.get('title')}: {action.get('command_or_step')}"
            )
        if action_lines:
            lines.append("\nNext actions:")
            lines.extend(action_lines[:3])

        lines.append(
            "\nAction Pack:\n```json\n"
            f"{json.dumps(pack, ensure_ascii=False, sort_keys=True)}\n```"
        )
        return "\n".join(lines)

    def _process_changes_requested_prs(self) -> None:
        """Find and retry PRs with changes requested."""
        prs = self.github.list_prs(labels=[self.STATUS_CHANGES_REQUESTED])

        if not prs:
            logger.debug("No PRs with changes requested")
            return

        logger.info(f"Found {len(prs)} PR(s) with changes requested")

        for pr in prs:
            if self._try_retry_pr(pr):
                logger.info(f"Successfully retried PR #{pr.number}")

    def _try_retry_pr(self, pr: PullRequest) -> bool:
        """
        Try to retry a PR with changes requested using TDD flow.

        Returns True if retried successfully, False otherwise.
        """
        logger.info(
            f"[{self.agent_id}] Attempting to retry PR #{pr.number}: {pr.title}"
        )

        # Resolve linked issue first; retry persistence is issue-based
        issue_number = self._extract_issue_number(pr.body)
        if not issue_number:
            logger.error(
                f"[{self.agent_id}] PR #{pr.number} has no linked issue; cannot retry safely"
            )
            self.github.comment_pr(
                pr.number,
                "⚠️ Cannot determine linked issue from PR body. "
                "Auto-retry has been stopped for safety.",
            )
            self.github.remove_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)
            self.github.add_pr_label(pr.number, self.STATUS_FAILED)
            return False

        # Get retry count from linked issue (with fallback to old PR marker)
        retry_count = self._get_retry_count(issue_number, pr.number)
        logger.info(
            f"[{self.agent_id}] Issue #{issue_number} retry count: {retry_count}/{self.MAX_RETRIES}"
        )

        if retry_count >= self.MAX_RETRIES:
            logger.warning(
                f"[{self.agent_id}] PR #{pr.number} exceeded max retries, escalating to Planner"
            )
            self.github.comment_issue(
                issue_number,
                f"⚠️ **Auto-retry failed after {self.MAX_RETRIES} attempts**\n\n"
                f"PR #{pr.number} could not pass review.\n\n"
                f"**Action needed**: Please review the specification and provide more details or clarification.\n\n"
                f"Review feedback:\n{self._get_latest_review_feedback(pr.number)}",
            )
            self._comment_worker_escalation(
                issue_number,
                "review-retry-limit",
                f"PR #{pr.number} exceeded retry limit ({self.MAX_RETRIES}).",
            )
            # Remove changes-requested label and add failed
            self.github.remove_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)
            self.github.add_pr_label(pr.number, self.STATUS_FAILED)
            return False

        # Try to acquire lock (changes-requested -> implementing)
        lock_result = self.lock.try_lock_pr(
            pr.number,
            self.STATUS_CHANGES_REQUESTED,
            self.STATUS_IMPLEMENTING,
        )

        if not lock_result.success:
            logger.debug(f"Could not lock PR #{pr.number}: {lock_result.error}")
            return False

        try:
            issue = self.github.get_issue(issue_number)
            if not issue:
                raise RuntimeError(f"Issue #{issue_number} not found")

            # Get review feedback
            feedback = self._get_latest_review_feedback(pr.number)

            # Prepare workspace
            logger.info(f"[{self.agent_id}] Preparing workspace...")
            clone_result = self.git.clone_or_pull()
            if not clone_result.success:
                raise RuntimeError(f"Failed to prepare workspace: {clone_result.error}")

            # Update the branch
            branch_name = pr.head_ref
            logger.info(f"[{self.agent_id}] Updating branch: {branch_name}")
            branch_result = self.git.checkout_branch_from_remote(branch_name)
            if not branch_result.success:
                raise RuntimeError(f"Failed to update branch: {branch_result.error}")

            self._ensure_retry_tests_available(issue, feedback)

            # TDD retry loop with test execution
            test_retry_count = 0
            validation_failure_output = ""
            test_passed = False

            while test_retry_count < self.MAX_RETRIES:
                logger.info(
                    f"[{self.agent_id}] Regenerating implementation with feedback "
                    f"(test attempt {test_retry_count + 1}/{self.MAX_RETRIES})..."
                )

                # Prepare spec with review feedback and test feedback
                impl_spec = f"{issue.body}\n\n## Review Feedback (Please address these issues)\n{feedback}"

                if test_retry_count > 0:
                    impl_spec += (
                        f"\n\n## Validation Failure (Attempt {test_retry_count})\n"
                        "Please fix the implementation to satisfy quality checks and tests.\n\n"
                        f"```\n{validation_failure_output[:2000]}\n```"
                    )

                # Generate improved implementation
                gen_result = self.llm.generate_implementation(
                    spec=impl_spec,
                    repo_context=f"Repository: {self.repo}\n"
                    f"Review retry: {retry_count + 1}/{self.MAX_RETRIES}\n"
                    f"Test attempt: {test_retry_count + 1}/{self.MAX_RETRIES}",
                    work_dir=self.git.path,
                )

                if not gen_result.success:
                    raise RuntimeError(f"Implementation failed: {gen_result.error}")

                # Commit changes
                logger.info(f"[{self.agent_id}] Committing changes...")
                commit_msg = (
                    f"fix: address review feedback for #{issue.number} "
                    f"(retry {retry_count + 1}, test attempt {test_retry_count + 1})\n\n"
                    f"{feedback[:500]}"
                )
                commit_result = self.git.commit(commit_msg)

                commit_status = classify_commit_result(
                    commit_result.success,
                    commit_result.output,
                    commit_result.error or "",
                )
                if commit_status == "failed":
                    raise RuntimeError(f"Commit failed: {commit_result.error}")
                if commit_status == "no_op":
                    commit_check = {
                        "name": "commit",
                        "exit_code": 1,
                        "result": "no_op",
                        "error_type": "commit_no_changes",
                        "primary_message": "No changes to commit",
                        "evidence": "No changes to commit",
                    }
                    self._latest_commit_action_pack = build_action_pack(
                        task=self._build_task_context(
                            issue.number, attempt=test_retry_count + 1
                        ),
                        phase="finalize",
                        status="no_op",
                        checks=[commit_check],
                        blockers=[],
                    )
                    self.github.comment_pr(
                        pr.number,
                        "ℹ️ **Retry commit resulted in no-op**\n\n"
                        f"{self._render_action_pack_comment(self._latest_commit_action_pack)}",
                    )
                    self.github.remove_pr_label(pr.number, self.STATUS_IMPLEMENTING)
                    self.github.add_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)
                    return False

                # Run quality gate before issue-specific tests
                logger.info(f"[{self.agent_id}] Running quality checks (ruff/mypy)...")
                quality_passed, quality_output = self._run_quality_checks(
                    git_path=self.git.path,
                    issue_number=issue_number,
                )
                if not quality_passed:
                    test_retry_count += 1
                    validation_failure_output = quality_output
                    logger.warning(
                        f"[{self.agent_id}] Quality checks failed (attempt {test_retry_count}/{self.MAX_RETRIES})"
                    )
                    quality_pack = self._latest_quality_action_pack
                    quality_body = (
                        self._render_action_pack_comment(quality_pack)
                        if quality_pack is not None
                        else quality_output[:2000]
                    )
                    self.github.comment_pr(
                        pr.number,
                        f"⚠️ **Quality checks failed during retry (attempt {test_retry_count}/{self.MAX_RETRIES})**\n\n"
                        "Retrying implementation with quality feedback...\n\n"
                        f"{quality_body}",
                    )
                    if test_retry_count >= self.MAX_RETRIES:
                        raise RuntimeError(
                            f"Quality checks failed after {self.MAX_RETRIES} attempts:\n{quality_output[:500]}"
                        )
                    continue

                # Transition to testing status
                self.github.remove_pr_label(pr.number, self.STATUS_IMPLEMENTING)
                self.github.add_pr_label(pr.number, self.STATUS_TESTING)

                # Run tests
                logger.info(f"[{self.agent_id}] Running tests...")
                test_passed, test_output = self._run_tests(
                    issue_number, git_path=self.git.path
                )

                if test_passed:
                    logger.info(
                        f"[{self.agent_id}] Tests passed on attempt {test_retry_count + 1}"
                    )
                    break

                # Tests failed - record and retry
                test_retry_count += 1
                validation_failure_output = test_output

                logger.warning(
                    f"[{self.agent_id}] Tests failed (attempt {test_retry_count}/{self.MAX_RETRIES})"
                )

                test_pack = self._latest_test_action_pack
                test_body = (
                    self._render_action_pack_comment(test_pack)
                    if test_pack is not None
                    else test_output[:2000]
                )
                # Comment on PR about test failure
                self.github.comment_pr(
                    pr.number,
                    f"⚠️ **Test failed during retry (attempt {test_retry_count}/{self.MAX_RETRIES})**\n\n"
                    f"Retrying implementation with test feedback...\n\n"
                    f"{test_body}",
                )

                if test_retry_count >= self.MAX_RETRIES:
                    raise RuntimeError(
                        f"Tests failed after {self.MAX_RETRIES} attempts:\n{test_output[:500]}"
                    )

                # Transition back to implementing for retry
                self.github.remove_pr_label(pr.number, self.STATUS_TESTING)
                self.github.add_pr_label(pr.number, self.STATUS_IMPLEMENTING)

            # Tests passed! Remove testing label
            self.github.remove_pr_label(pr.number, self.STATUS_TESTING)

            # Force push to update PR
            logger.info(f"[{self.agent_id}] Force pushing to update PR...")
            push_result = self.git.push(branch_name, force=True)
            if not push_result.success:
                raise RuntimeError(f"Push failed: {push_result.error}")

            # Record retry
            self._record_retry(issue_number, pr.number, retry_count + 1)

            # Add reviewing label
            self.github.add_pr_label(pr.number, self.STATUS_REVIEWING)

            logger.info(
                f"[{self.agent_id}] PR #{pr.number} updated with TDD, ready for re-review"
            )

            # Wait for CI and handle failures with retry loop
            logger.info(f"[{self.agent_id}] Waiting for CI to complete...")
            ci_passed, ci_status = self._wait_for_ci(
                pr.number, timeout=self.CI_WAIT_TIMEOUT
            )

            ci_retry_count = 0
            while (
                not ci_passed
                and ci_status == "failure"
                and ci_retry_count < self.MAX_CI_RETRIES
            ):
                ci_retry_count += 1
                logger.warning(
                    f"[{self.agent_id}] CI failed (attempt {ci_retry_count}/{self.MAX_CI_RETRIES})"
                )

                # Get CI failure logs
                ci_logs = self._get_ci_failure_logs(pr.number)

                # Comment about CI failure
                self.github.comment_pr(
                    pr.number,
                    f"⚠️ **CI failed (attempt {ci_retry_count}/{self.MAX_CI_RETRIES})**\n\n"
                    f"Analyzing failures and attempting automatic fix...\n\n"
                    f"<details>\n<summary>CI failure details</summary>\n\n"
                    f"{ci_logs[:2000]}\n\n</details>",
                )

                # Ask LLM to fix CI failures
                fix_spec = f"""{issue.body}

## Review Feedback
{feedback}

## CI Failure (Attempt {ci_retry_count})
The implementation passed local tests but CI checks failed.
Please analyze and fix the CI failures.

{ci_logs}
"""

                logger.info(
                    f"[{self.agent_id}] Asking LLM to fix CI failures (attempt {ci_retry_count})..."
                )
                fix_result = self.llm.generate_implementation(
                    spec=fix_spec,
                    repo_context=f"Repository: {self.repo}\nCI fix attempt: {ci_retry_count}/{self.MAX_CI_RETRIES}",
                    work_dir=self.git.path,
                )

                if not fix_result.success:
                    logger.error(
                        f"[{self.agent_id}] CI fix generation failed: {fix_result.error}"
                    )
                    break

                # Commit CI fix
                fix_commit_msg = f"fix: address CI failures for PR #{pr.number} (attempt {ci_retry_count})\n\n{ci_logs[:200]}"
                fix_commit_result = self.git.commit(fix_commit_msg)

                if not fix_commit_result.success:
                    logger.error(
                        f"[{self.agent_id}] CI fix commit failed: {fix_commit_result.error}"
                    )
                    break

                # Push fix (force push to update PR)
                push_result = self.git.push(branch_name, force=True)
                if not push_result.success:
                    logger.error(
                        f"[{self.agent_id}] CI fix push failed: {push_result.error}"
                    )
                    break

                # Wait for CI again
                logger.info(f"[{self.agent_id}] Waiting for CI after fix...")
                ci_passed, ci_status = self._wait_for_ci(
                    pr.number, timeout=self.CI_WAIT_TIMEOUT
                )

            # Check final CI status
            if not ci_passed:
                if ci_status == "pending":
                    # CI still pending after timeout
                    logger.warning(
                        f"[{self.agent_id}] CI still pending after timeout, marking for manual review"
                    )
                    self.github.comment_pr(
                        pr.number,
                        "⏱️ **CI checks are taking longer than expected**\n\n"
                        "The PR is ready for manual review while CI completes.",
                    )
                elif ci_status == "failure":
                    # CI failed after all retries
                    logger.error(
                        f"[{self.agent_id}] CI failed after {ci_retry_count} fix attempts"
                    )

                    # Mark PR with ci-failed status
                    self.github.add_pr_label(pr.number, self.STATUS_CI_FAILED)
                    self.github.add_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)
                    self.github.remove_pr_label(pr.number, self.STATUS_REVIEWING)

                    self.github.comment_pr(
                        pr.number,
                        f"❌ **CI checks failed after {ci_retry_count} automatic fix attempts**\n\n"
                        f"Manual intervention required. Please review the CI logs and fix the issues.\n\n"
                        f"The PR has been marked with `{self.STATUS_CI_FAILED}` label.",
                    )

                    return False
            else:
                logger.info(f"[{self.agent_id}] CI passed!")
                if ci_retry_count > 0:
                    self.github.comment_pr(
                        pr.number,
                        f"✅ **CI now passing after {ci_retry_count} automatic fix(es)!**",
                    )

            # Comment on PR with final status
            ci_info = ""
            if ci_retry_count > 0:
                ci_info = f"\n- CI fixes: {ci_retry_count} automatic fix(es) applied"

            self.github.comment_pr(
                pr.number,
                f"✅ **Implementation updated with TDD!**\n\n"
                f"- Addressed review feedback\n"
                f"- All tests passed (attempts: {test_retry_count + 1}/{self.MAX_RETRIES}){ci_info}\n"
                f"- Ready for re-review\n\n"
                f"Review retry: {retry_count + 1}/{self.MAX_RETRIES}",
            )

            return True

        except Exception as e:
            logger.error(f"[{self.agent_id}] Failed to retry PR #{pr.number}: {e}")

            # Always record a failed retry attempt so we don't loop forever
            failed_retry_count = retry_count + 1
            self._record_retry(
                issue_number, pr.number, failed_retry_count, error=str(e)
            )

            # If retry budget is exhausted, stop auto-retry and escalate
            if failed_retry_count >= self.MAX_RETRIES:
                self.github.comment_issue(
                    issue_number,
                    f"⚠️ **Auto-retry failed after {self.MAX_RETRIES} attempts**\n\n"
                    f"PR #{pr.number} could not be stabilized due to repeated retry errors.\n\n"
                    "Please review the specification and/or implementation strategy.",
                )
                self._comment_worker_escalation(
                    issue_number,
                    "review-retry-error-limit",
                    f"PR #{pr.number} retry flow failed repeatedly and exhausted retry budget.",
                )
                self.github.remove_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)
                self.github.remove_pr_label(pr.number, self.STATUS_IMPLEMENTING)
                self.github.remove_pr_label(pr.number, self.STATUS_TESTING)
                self.github.add_pr_label(pr.number, self.STATUS_FAILED)
                return False

            # Otherwise, reset to changes-requested for the next retry cycle
            self.github.remove_pr_label(pr.number, self.STATUS_IMPLEMENTING)
            self.github.remove_pr_label(pr.number, self.STATUS_TESTING)
            self.github.add_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)

            self.github.comment_pr(
                pr.number,
                f"⚠️ Auto-retry failed: {e}\n\nPlease review manually or update the specification.",
            )

            return False

    def _run_tests(
        self, issue_number: int, git_path: Path | None = None
    ) -> tuple[bool, str]:
        """
        Run pytest for the specific issue's tests.

        Args:
            issue_number: The issue number

        Returns:
            (success, output): 成功フラグとテスト出力
        """
        repo_path = git_path or self.git.path
        test_path = self._locate_issue_test_file(issue_number, repo_path)

        if test_path is None:
            message = f"Test file not found: tests/test_issue_{issue_number}.py"
            check = parse_pytest_output(message, 1)
            self._latest_test_action_pack = build_action_pack(
                task=self._build_task_context(issue_number),
                phase="test",
                status="failed",
                checks=[check],
                blockers=[{"type": "missing_test_file", "message": message}],
            )
            return False, message

        test_file = str(test_path.relative_to(repo_path))

        logger.info(f"[{self.agent_id}] Running tests: {test_file}")

        try:
            result = subprocess.run(
                ["pytest", test_file, "-v", "--tb=short"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=300,  # 5分タイムアウト
            )

            output = f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
            success = result.returncode == 0
            check = parse_pytest_output(output, result.returncode)
            self._latest_test_action_pack = build_action_pack(
                task=self._build_task_context(issue_number),
                phase="test",
                status="ok" if success else "failed",
                checks=[check],
                blockers=[],
            )

            if success:
                logger.info(f"[{self.agent_id}] Tests passed for issue #{issue_number}")
            else:
                logger.warning(
                    f"[{self.agent_id}] Tests failed for issue #{issue_number}"
                )

            return success, output

        except subprocess.TimeoutExpired:
            error_msg = "Tests timed out after 5 minutes"
            logger.error(f"[{self.agent_id}] {error_msg}")
            check = parse_pytest_output(error_msg, 1)
            self._latest_test_action_pack = build_action_pack(
                task=self._build_task_context(issue_number),
                phase="test",
                status="failed",
                checks=[check],
                blockers=[{"type": "timeout", "message": error_msg}],
            )
            return False, error_msg
        except Exception as e:
            error_msg = f"Test execution error: {str(e)}"
            logger.error(f"[{self.agent_id}] {error_msg}")
            check = parse_pytest_output(error_msg, 1)
            self._latest_test_action_pack = build_action_pack(
                task=self._build_task_context(issue_number),
                phase="test",
                status="failed",
                checks=[check],
                blockers=[{"type": "exception", "message": error_msg}],
            )
            return False, error_msg

    def _run_quality_checks(
        self, git_path: Path | None = None, issue_number: int = 0
    ) -> tuple[bool, str]:
        """Run repo-wide quality checks before issue-specific tests."""
        repo_path = git_path or self.git.path
        checks = [
            ("ruff", ["uv", "run", "ruff", "check", "."]),
            ("mypy", ["uv", "run", "mypy", "."]),
        ]
        outputs: list[str] = []
        check_results = []
        blockers = []
        all_passed = True

        for check_name, cmd in checks:
            try:
                result = subprocess.run(
                    cmd,
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                outputs.append(
                    f"{check_name} (exit={result.returncode})\n"
                    f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
                )
                raw_output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
                if check_name == "ruff":
                    check_results.append(
                        parse_ruff_output(raw_output, result.returncode)
                    )
                else:
                    check_results.append(
                        parse_mypy_output(raw_output, result.returncode)
                    )
                if result.returncode != 0:
                    all_passed = False
            except subprocess.TimeoutExpired:
                all_passed = False
                timeout_message = f"{check_name} timed out after 5 minutes"
                outputs.append(timeout_message)
                blockers.append({"type": "timeout", "message": timeout_message})
                if check_name == "ruff":
                    check_results.append(parse_ruff_output(timeout_message, 1))
                else:
                    check_results.append(parse_mypy_output(timeout_message, 1))
            except Exception as e:
                all_passed = False
                error_message = f"{check_name} execution error: {e}"
                outputs.append(error_message)
                blockers.append({"type": "exception", "message": error_message})
                if check_name == "ruff":
                    check_results.append(parse_ruff_output(error_message, 1))
                else:
                    check_results.append(parse_mypy_output(error_message, 1))

        self._latest_quality_action_pack = build_action_pack(
            task=self._build_task_context(issue_number=issue_number),
            phase="quality",
            status="ok" if all_passed else "failed",
            checks=check_results,
            blockers=blockers,
        )

        return all_passed, "\n\n".join(outputs)

    def _snapshot_test_files(self, repo_path: Path) -> set[Path]:
        """Collect current test files under tests/."""
        tests_dir = repo_path / "tests"
        if not tests_dir.exists():
            return set()
        return {path for path in tests_dir.rglob("test*.py") if path.is_file()}

    def _ensure_issue_test_file(
        self, issue_number: int, repo_path: Path, before_files: set[Path]
    ) -> None:
        """Ensure generated issue test exists at tests/test_issue_<n>.py."""
        expected = repo_path / "tests" / f"test_issue_{issue_number}.py"
        if expected.exists():
            return

        after_files = self._snapshot_test_files(repo_path)
        new_files = sorted(
            [path for path in after_files if path not in before_files],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

        if len(new_files) == 1:
            expected.parent.mkdir(parents=True, exist_ok=True)
            source = new_files[0]
            logger.warning(
                f"[{self.agent_id}] Generated test file {source.relative_to(repo_path)} "
                f"does not match required path; moving to tests/test_issue_{issue_number}.py"
            )
            source.replace(expected)
            return

        logger.warning(
            f"[{self.agent_id}] Could not normalize generated tests to "
            f"tests/test_issue_{issue_number}.py automatically."
        )

    def _ensure_retry_tests_available(self, issue: Issue, feedback: str) -> None:
        """
        Ensure retry flow has an issue-specific test file to execute.

        When tests are missing on the PR branch, regenerate tests before retrying
        implementation so the fix loop remains deterministic.
        """
        if self._locate_issue_test_file(issue.number, self.git.path) is not None:
            return

        logger.warning(
            f"[{self.agent_id}] Missing tests/test_issue_{issue.number}.py on retry branch. "
            "Regenerating tests before implementation retry."
        )
        before_files = self._snapshot_test_files(self.git.path)
        test_result = self.llm.generate_tests(
            spec=(
                f"{issue.body}\n\n"
                "## Review Feedback Context\n"
                f"{feedback}\n\n"
                "## Test File Requirement\n"
                f"Create/overwrite exactly one issue test file at "
                f"`tests/test_issue_{issue.number}.py`."
            ),
            repo_context=(
                f"Repository: {self.repo}\n"
                f"Required test path: tests/test_issue_{issue.number}.py"
            ),
            work_dir=self.git.path,
        )
        if not test_result.success:
            raise RuntimeError(
                f"Retry test generation failed: {test_result.error or 'unknown error'}"
            )

        self._ensure_issue_test_file(issue.number, self.git.path, before_files)
        if self._locate_issue_test_file(issue.number, self.git.path) is None:
            raise RuntimeError(
                f"Retry tests still missing after generation: tests/test_issue_{issue.number}.py"
            )

        commit_result = self.git.commit(
            f"test: refresh tests for #{issue.number} before retry\n\n{issue.title}"
        )
        if not commit_result.success and commit_result.error != "No changes to commit":
            raise RuntimeError(f"Retry test commit failed: {commit_result.error}")

    def _locate_issue_test_file(
        self, issue_number: int, repo_path: Path
    ) -> Path | None:
        """Locate the issue test file, preferring the canonical filename."""
        expected = repo_path / "tests" / f"test_issue_{issue_number}.py"
        if expected.exists():
            return expected

        tests_dir = repo_path / "tests"
        if not tests_dir.exists():
            return None

        candidates = [
            path
            for path in tests_dir.rglob("test*.py")
            if path.is_file() and str(issue_number) in path.stem
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _wait_for_ci(
        self, pr_number: int, timeout: int | None = None
    ) -> tuple[bool, str]:
        """
        Wait for CI to complete (max timeout seconds).

        Args:
            pr_number: PR number
            timeout: Maximum wait time in seconds (default: CI_WAIT_TIMEOUT)

        Returns:
            (passed, status):
                - (True, "success") - CI passed
                - (False, "pending") - CI timeout (still pending)
                - (False, "failure") - CI failed
        """
        if timeout is None:
            timeout = self.CI_WAIT_TIMEOUT

        logger.info(
            f"[{self.agent_id}] Waiting for CI on PR #{pr_number} (timeout: {timeout}s)"
        )

        elapsed = 0
        while elapsed < timeout:
            ci_status = self.github.get_ci_status(pr_number)

            if ci_status["status"] == "success":
                logger.info(f"[{self.agent_id}] CI passed on PR #{pr_number}")
                return True, "success"
            elif ci_status["status"] == "failure":
                logger.warning(f"[{self.agent_id}] CI failed on PR #{pr_number}")
                return False, "failure"
            elif ci_status["status"] == "none":
                logger.info(f"[{self.agent_id}] No CI configured for PR #{pr_number}")
                return True, "success"  # No CI means pass

            # Still pending, wait and check again
            pending_count = ci_status["pending_count"]
            logger.debug(
                f"[{self.agent_id}] CI pending on PR #{pr_number} "
                f"({pending_count} checks pending, elapsed: {elapsed}s)"
            )

            time.sleep(self.CI_CHECK_INTERVAL)
            elapsed += self.CI_CHECK_INTERVAL

        # Timeout
        logger.warning(
            f"[{self.agent_id}] CI timeout on PR #{pr_number} after {timeout}s"
        )
        return False, "pending"

    def _get_ci_failure_logs(self, pr_number: int) -> str:
        """
        Get formatted CI failure logs for LLM.

        Args:
            pr_number: PR number

        Returns:
            Formatted CI error logs
        """
        logger.info(f"[{self.agent_id}] Fetching CI failure logs for PR #{pr_number}")

        failed_checks = self.github.get_ci_logs(pr_number)

        if not failed_checks:
            return "CI failed but no detailed logs available."

        # Format logs for LLM
        logs = []
        logs.append("# CI Failure Report\n")

        for check in failed_checks:
            logs.append(f"\n## Check: {check['name']}")
            logs.append(f"**Status:** {check['conclusion']}")

            if "html_url" in check:
                logs.append(f"**URL:** {check['html_url']}")

            if "output" in check and check["output"]:
                output = check["output"]
                if output.get("title"):
                    logs.append(f"\n**Error:** {output['title']}")
                if output.get("summary"):
                    logs.append(f"\n**Details:**\n```\n{output['summary'][:1000]}\n```")

        return "\n".join(logs)

    def _extract_issue_number(self, pr_body: str) -> int | None:
        """Extract issue number from PR body."""
        patterns = [
            r"[Cc]loses?\s+#(\d+)",
            r"[Ff]ixes?\s+#(\d+)",
            r"[Rr]esolves?\s+#(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, pr_body)
            if match:
                return int(match.group(1))

        return None

    def _get_retry_count(self, issue_number: int, pr_number: int | None = None) -> int:
        """Get current retry count from Issue comments (fallback to legacy PR marker)."""
        comments = self.github.get_issue_comments(issue_number, limit=100)
        retry_count = 0

        for comment in comments:
            body = comment.get("body", "")
            # Primary marker: WORKER_RETRY:N
            match = re.search(rf"{self.RETRY_MARKER}:(\d+)", body)
            # Backward compatibility with legacy RETRY:N marker
            if not match:
                match = re.search(r"RETRY:(\d+)", body)
            if match:
                count = int(match.group(1))
                retry_count = max(retry_count, count)

        # Migration fallback: also read legacy PR comment marker if provided
        if pr_number is not None and pr_number != issue_number:
            pr_comments = self.github.get_issue_comments(pr_number, limit=50)
            for comment in pr_comments:
                body = comment.get("body", "")
                match = re.search(r"RETRY:(\d+)", body)
                if match:
                    count = int(match.group(1))
                    retry_count = max(retry_count, count)

        return retry_count

    def _record_retry(
        self,
        issue_number: int,
        pr_number: int,
        retry_count: int,
        error: str | None = None,
    ) -> None:
        """Record retry attempt in linked Issue comment for persistence."""
        body = (
            f"🔄 **Auto-retry #{retry_count}**\n\n"
            f"{self.RETRY_MARKER}:{retry_count}\n\n"
            f"Linked PR: #{pr_number}\n\n"
            "Worker Agent is addressing review feedback and regenerating implementation."
        )
        if error:
            body += f"\n\nError: {error}"
        self.github.comment_issue(issue_number, body)

    def _get_latest_review_feedback(self, pr_number: int) -> str:
        """Get the most recent review feedback."""
        reviews = self.github.get_pr_reviews(pr_number)

        if not reviews:
            return "No review feedback available"

        # Get the most recent CHANGES_REQUESTED review
        for review in reversed(reviews):
            if review.get("state") == "CHANGES_REQUESTED":
                body = review.get("body", "").strip()
                if body:
                    return str(body)

        # Fallback to the most recent review with a body
        for review in reversed(reviews):
            body = review.get("body", "").strip()
            if body:
                return str(body)

        return "No detailed review feedback available"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Worker Agent - Autonomous implementation daemon"
    )
    parser.add_argument(
        "repo",
        help="Repository in owner/repo format",
    )
    parser.add_argument(
        "--config",
        "-c",
        help="Path to config file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one issue and exit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    agent = WorkerAgent(args.repo, config_path=args.config)

    if args.once:
        success = agent.run_once()
        sys.exit(0 if success else 1)
    else:
        agent.run()


if __name__ == "__main__":
    main()
