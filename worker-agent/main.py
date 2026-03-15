#!/usr/bin/env python3
"""Worker Agent - Autonomous implementation daemon."""

import argparse
import logging
import sys
import time
import uuid
from collections.abc import Generator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import get_agent_config
from shared.git_operations import GitOperations
from shared.github_client import GitHubClient, Issue, PullRequest
from shared.llm_client import LLMClient
from shared.lock import LockManager
from shared.workspace import WorkspaceManager
from workflow_engine import worker_common, worker_issue_flow, worker_retry_flow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("worker-agent")


class WorkerAgent:
    """Autonomous worker that implements issues."""

    STATUS_READY = "status:ready"
    STATUS_IMPLEMENTING = "status:implementing"
    STATUS_TESTING = "status:testing"
    STATUS_REVIEWING = "status:reviewing"
    STATUS_CHANGES_REQUESTED = "status:changes-requested"
    STATUS_FAILED = "status:failed"
    STATUS_CI_FAILED = "status:ci-failed"
    STATUS_NEEDS_CLARIFICATION = "status:needs-clarification"
    RETRY_MARKER = "WORKER_RETRY"

    MAX_RETRIES = 3
    MAX_CI_RETRIES = 3

    CI_WAIT_TIMEOUT = 600
    CI_CHECK_INTERVAL = 30
    CI_NO_CHECKS_GRACE_SECONDS = 60
    CI_POST_PUSH_SETTLE_SECONDS = 15

    MIN_SPEC_LENGTH = 100

    CODE_QUALITY_REQUIREMENTS = (
        "\n\n## Code Quality Requirements\n"
        "- All public functions, methods, and classes MUST have docstrings.\n"
        "- Docstring coverage must be at least 80% to pass CI checks.\n"
        "- Use Google-style or reStructuredText docstrings consistently.\n"
    )
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

        self.agent_id = f"worker-{uuid.uuid4().hex[:8]}"
        self.github = GitHubClient(repo, gh_cli=self.config.gh_cli)
        self.lock = LockManager(
            self.github, agent_type="worker", agent_id=self.agent_id
        )
        self.llm = LLMClient(self.config)
        self.git = GitOperations(repo, Path(self.config.work_dir))
        self.workspace_manager = WorkspaceManager(repo, self.git.path)

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
                time.sleep(60)

    def run_once(self) -> bool:
        """Process one issue or PR and return. For testing."""
        if self._process_stale_locks():
            return True

        issues = self.github.list_issues(labels=[self.STATUS_READY])
        if issues:
            return self._try_process_issue(issues[0])

        prs = self.github.list_prs(labels=[self.STATUS_CHANGES_REQUESTED])
        if prs:
            return self._try_retry_pr(prs[0])

        logger.info("No work to process")
        return False

    def _get_stale_timeout_minutes(self) -> int:
        return worker_common.get_stale_timeout_minutes(self)

    def _parse_github_datetime(self, ts: str | None):
        return worker_common.parse_github_datetime(self, ts)

    def _get_lock_reference_time(self, number: int):
        return worker_common.get_lock_reference_time(self, number)

    def _is_stale_lock(self, number: int) -> tuple[bool, float | None]:
        return worker_common.is_stale_lock(self, number)

    def _get_pr_recovery_status(self, pr_number: int) -> str:
        return worker_common.get_pr_recovery_status(self, pr_number)

    def _process_stale_locks(self) -> bool:
        return worker_common.process_stale_locks(self)

    def _process_ready_issues(self) -> None:
        worker_issue_flow.process_ready_issues(self)

    def _issue_workspace(
        self, issue_number: int, branch_name: str
    ) -> Generator[GitOperations, None, None]:
        return worker_issue_flow.issue_workspace(self, issue_number, branch_name)

    def _try_process_issue(self, issue: Issue) -> bool:
        return worker_issue_flow.try_process_issue(self, issue)

    def _is_specification_unclear(self, failure_reason: str, spec: str) -> bool:
        return worker_common.is_specification_unclear(self, failure_reason, spec)

    def _generate_planner_feedback(
        self,
        issue_number: int,
        spec: str,
        failure_reason: str,
        attempt_count: int,
    ) -> str:
        return worker_common.generate_planner_feedback(
            self,
            issue_number,
            spec,
            failure_reason,
            attempt_count,
        )

    def _comment_worker_escalation(
        self, issue_number: int, reason: str, details: str
    ) -> None:
        worker_common.comment_worker_escalation(self, issue_number, reason, details)

    def _process_changes_requested_prs(self) -> None:
        worker_retry_flow.process_changes_requested_prs(self)

    def _try_retry_pr(self, pr: PullRequest) -> bool:
        return worker_retry_flow.try_retry_pr(self, pr)

    def _run_tests(
        self, issue_number: int, git_path: Path | None = None
    ) -> tuple[bool, str]:
        return worker_common.run_tests(self, issue_number, git_path)

    def _auto_format(self, repo_path: Path) -> None:
        worker_common.auto_format(self, repo_path)

    def _run_quality_checks(self, git_path: Path | None = None) -> tuple[bool, str]:
        return worker_common.run_quality_checks(self, git_path)

    def _snapshot_test_files(self, repo_path: Path) -> set[Path]:
        return worker_common.snapshot_test_files(self, repo_path)

    def _ensure_issue_test_file(
        self, issue_number: int, repo_path: Path, before_files: set[Path]
    ) -> None:
        worker_common.ensure_issue_test_file(
            self, issue_number, repo_path, before_files
        )

    def _ensure_retry_tests_available(self, issue: Issue, feedback: str) -> None:
        worker_common.ensure_retry_tests_available(self, issue, feedback)

    def _locate_issue_test_file(
        self, issue_number: int, repo_path: Path
    ) -> Path | None:
        return worker_common.locate_issue_test_file(self, issue_number, repo_path)

    def _wait_for_ci(
        self, pr_number: int, timeout: int | None = None
    ) -> tuple[bool, str]:
        return worker_common.wait_for_ci(self, pr_number, timeout)

    def _get_ci_failure_logs(self, pr_number: int) -> str:
        return worker_common.get_ci_failure_logs(self, pr_number)

    def _extract_issue_number(self, pr_body: str) -> int | None:
        return worker_common.extract_issue_number(self, pr_body)

    def _get_retry_count(self, issue_number: int, pr_number: int | None = None) -> int:
        return worker_common.get_retry_count(self, issue_number, pr_number)

    def _record_retry(
        self,
        issue_number: int,
        pr_number: int,
        retry_count: int,
        error: str | None = None,
    ) -> None:
        worker_common.record_retry(self, issue_number, pr_number, retry_count, error)

    def _get_latest_review_feedback(self, pr_number: int) -> str:
        return worker_common.get_latest_review_feedback(self, pr_number)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Worker Agent - Autonomous implementation daemon"
    )
    parser.add_argument("repo", help="Repository in owner/repo format")
    parser.add_argument("--config", "-c", help="Path to config file")
    parser.add_argument(
        "--once", action="store_true", help="Process one issue and exit"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    agent = WorkerAgent(args.repo, config_path=args.config)
    if args.once:
        success = agent.run_once()
        sys.exit(0 if success else 1)

    agent.run()


if __name__ == "__main__":
    main()
