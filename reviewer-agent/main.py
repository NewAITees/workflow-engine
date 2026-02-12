#!/usr/bin/env python3
"""Reviewer Agent - Autonomous code review daemon.

Watches for PRs with 'status:reviewing' label and automatically
reviews them using the configured LLM backend (codex or claude).
"""

import argparse
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import cast

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import get_agent_config
from shared.github_client import GitHubClient, Issue, PullRequest
from shared.llm_client import LLMClient
from shared.lock import LockManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("reviewer-agent")


class IssueSeverity(Enum):
    """Issue severity classification."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    TRIVIAL = "trivial"


class ReviewerAgent:
    """Autonomous reviewer that reviews PRs."""

    # Status labels
    STATUS_REVIEWING = "status:reviewing"
    STATUS_IN_REVIEW = "status:in-review"
    STATUS_APPROVED = "status:approved"
    STATUS_CHANGES_REQUESTED = "status:changes-requested"

    # Accumulation settings
    ACCUMULATED_THRESHOLD = 5
    ACCUMULATED_DIR = Path.home() / ".workflow-engine" / "accumulated_fixes"

    def __init__(
        self,
        repo: str,
        config_path: str | None = None,
        dry_run: str | None = None,
    ):
        self.repo = repo
        self.config = get_agent_config(repo, config_path)
        if dry_run is not None:
            self.config.dry_run = dry_run

        # Generate unique agent ID
        self.agent_id = f"reviewer-{uuid.uuid4().hex[:8]}"

        # Initialize components
        self.github = GitHubClient(
            repo, gh_cli=self.config.gh_cli, dry_run=self.config.dry_run
        )
        self.lock = LockManager(
            self.github, agent_type="reviewer", agent_id=self.agent_id
        )
        self.llm = LLMClient(self.config)

        self.accumulated_fixes_dir = self.ACCUMULATED_DIR / repo.replace("/", "-")
        self.accumulated_fixes_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Reviewer Agent initialized for {repo}")
        logger.info(f"Agent ID: {self.agent_id}")
        logger.info(f"LLM backend: {self.config.llm_backend}")

    def run(self) -> None:
        """Main daemon loop."""
        logger.info(f"Starting Reviewer Agent daemon for {self.repo}")
        logger.info(f"Poll interval: {self.config.poll_interval}s")

        while True:
            try:
                self._process_reviewing_prs()
                time.sleep(self.config.poll_interval)

            except KeyboardInterrupt:
                logger.info("Shutting down Reviewer Agent")
                break
            except Exception as e:
                logger.exception(f"Unexpected error: {e}")
                time.sleep(60)

    def run_once(self) -> bool:
        """Process one PR and return. For testing."""
        prs = self.github.list_prs(labels=[self.STATUS_REVIEWING])
        if not prs:
            logger.info("No PRs to review")
            return False

        return self._try_review_pr(prs[0])

    def _process_reviewing_prs(self) -> None:
        """Find and process PRs ready for review."""
        prs = self.github.list_prs(labels=[self.STATUS_REVIEWING])

        if not prs:
            logger.debug("No PRs to review")
            return

        logger.info(f"Found {len(prs)} PR(s) to review")

        for pr in prs:
            # Check if CI has passed
            if not self.github.is_ci_green(pr.number):
                logger.debug(f"PR #{pr.number} CI not green, skipping")
                continue

            if self._try_review_pr(pr):
                logger.info(f"Successfully reviewed PR #{pr.number}")

    def _try_review_pr(self, pr: PullRequest) -> bool:
        """
        Try to review a single PR.

        Returns True if reviewed successfully.
        """
        logger.info(f"Attempting to review PR #{pr.number}: {pr.title}")

        # Try to acquire lock
        lock_result = self.lock.try_lock_pr(
            pr.number,
            self.STATUS_REVIEWING,
            self.STATUS_IN_REVIEW,
        )

        if not lock_result.success:
            logger.debug(f"Could not lock PR #{pr.number}: {lock_result.error}")
            return False

        try:
            linked_issue = self._find_linked_issue(pr)
            spec = linked_issue.body if linked_issue else self._get_linked_spec(pr)

            diff = self.github.get_pr_diff(pr.number)
            if not diff:
                raise RuntimeError("Failed to get PR diff")

            logger.info(
                f"[{self.agent_id}] Reviewing PR #{pr.number} with severity-based LLM..."
            )
            review_result = self.llm.review_code_with_severity(
                spec=spec,
                diff=diff,
                repo_context=f"Repository: {self.repo}",
                work_dir=Path.cwd(),
            )

            if not review_result.success:
                raise RuntimeError(f"Review failed: {review_result.error}")

            try:
                review_data = json.loads(review_result.output)
            except json.JSONDecodeError:
                raise RuntimeError("Failed to parse review result as JSON")

            issues = review_data.get("issues", [])
            summary = review_data.get("summary", "").strip()

            critical_major = [
                issue
                for issue in issues
                if issue.get("severity")
                in {
                    IssueSeverity.CRITICAL.value,
                    IssueSeverity.MAJOR.value,
                }
            ]
            minor_trivial = [
                issue
                for issue in issues
                if issue.get("severity")
                in {
                    IssueSeverity.MINOR.value,
                    IssueSeverity.TRIVIAL.value,
                }
            ]

            if critical_major:
                logger.info(
                    f"[{self.agent_id}] Found {len(critical_major)} critical/major issues, requesting changes"
                )
                feedback = "## Critical/Major Issues\n\n"
                for issue_data in critical_major:
                    feedback += f"**[{issue_data['severity'].upper()}] {issue_data['file']}:{issue_data['line']}**\n"
                    feedback += f"- {issue_data['description']}\n"
                    feedback += f"- Suggestion: {issue_data['suggestion']}\n\n"

                feedback += f"\n## Summary\n{summary}"

                self.github.remove_pr_label(pr.number, self.STATUS_IN_REVIEW)
                self.github.add_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)
                self.github.request_changes_pr(pr.number, feedback)
                return True

            if minor_trivial:
                logger.info(
                    f"[{self.agent_id}] Found {len(minor_trivial)} minor/trivial issues, accumulating..."
                )

                threshold_reached = False
                for issue_data in minor_trivial:
                    if self._add_accumulated_issue(
                        pr.number,
                        issue_data,
                        issue_number=(linked_issue.number if linked_issue else None),
                    ):
                        threshold_reached = True

                if threshold_reached:
                    logger.info(
                        f"[{self.agent_id}] Threshold reached, sending accumulated feedback"
                    )
                    feedback = self._format_accumulated_feedback(pr.number)
                    feedback += f"\n\n## Summary\n{summary}\n\n"
                    feedback += "These are accumulated minor/trivial issues. Please address them when convenient."

                    self.github.remove_pr_label(pr.number, self.STATUS_IN_REVIEW)
                    self.github.add_pr_label(pr.number, self.STATUS_CHANGES_REQUESTED)
                    self.github.request_changes_pr(pr.number, feedback)
                    self._clear_accumulated_fixes(pr.number)
                    return True
                else:
                    accumulated = self._load_accumulated_fixes(
                        pr.number,
                        issue_number=(linked_issue.number if linked_issue else None),
                    )
                    current = accumulated.get("current_count", 0)
                    threshold = accumulated.get("threshold", self.ACCUMULATED_THRESHOLD)
                    comment = (
                        f"✅ **Approved with {len(minor_trivial)} minor/trivial issues noted**\n\n"
                        f"Issues are being accumulated ({current}/{threshold}). "
                        "You'll receive consolidated feedback when the threshold is reached.\n\n"
                        f"Summary: {summary}"
                    )
                    self.github.comment_pr(pr.number, comment)

            logger.info(f"PR #{pr.number} approved")
            self.github.remove_pr_label(pr.number, self.STATUS_IN_REVIEW)
            self.github.add_pr_label(pr.number, self.STATUS_APPROVED)
            approve_body = f"## Auto-Review by Reviewer Agent ({self.config.llm_backend})\n\n{summary}\n\n✅ Code review passed!"
            self.github.approve_pr(pr.number, approve_body)

            if self.config.auto_merge:
                logger.info(
                    f"Auto-merging PR #{pr.number} with method: {self.config.merge_method}"
                )
                if self.github.merge_pr(pr.number, method=self.config.merge_method):
                    logger.info(f"Successfully merged PR #{pr.number}")
                else:
                    logger.error(f"Failed to auto-merge PR #{pr.number}")
                    self.github.comment_pr(
                        pr.number, "⚠️ Auto-merge failed. Please merge manually."
                    )

            return True

        except Exception as e:
            logger.error(f"Failed to review PR #{pr.number}: {e}")

            # Reset status
            self.github.remove_pr_label(pr.number, self.STATUS_IN_REVIEW)
            self.github.add_pr_label(pr.number, self.STATUS_REVIEWING)

            self.github.comment_pr(
                pr.number,
                f"⚠️ Auto-review failed: {e}\n\nPlease review manually.",
            )

            return False

    def _find_linked_issue(self, pr: PullRequest) -> Issue | None:
        """Find the linked issue for a PR."""
        patterns = [
            r"[Cc]loses?\s+#(\d+)",
            r"[Ff]ixes?\s+#(\d+)",
            r"[Rr]esolves?\s+#(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, pr.body)
            if match:
                issue_number = int(match.group(1))
                issue = self.github.get_issue(issue_number)
                if issue:
                    return issue
        return None

    def _get_linked_spec(self, pr: PullRequest) -> str:
        """Extract spec from linked issue."""
        issue = self._find_linked_issue(pr)
        if issue:
            return issue.body

        logger.warning("No linked issue found, using PR body as spec")
        return pr.body

    def _review_code(self, spec: str, diff: str) -> dict:
        """
        Review code using LLM.

        Returns dict with 'approved' bool and 'comment' string.
        """
        # Truncate very large diffs
        truncated_diff = diff[:50000]

        result = self.llm.review_code(spec, truncated_diff)

        if not result.success:
            logger.error(f"LLM review failed: {result.error}")
            return {
                "approved": False,
                "comment": f"Auto-review encountered an error: {result.error}\n\nPlease review manually.",
            }

        output = result.output

        # Parse decision - look for DECISION line or ### Decision header
        approved = False
        lines = output.split("\n")
        for i, line in enumerate(lines):
            line_upper = line.strip().upper()

            # Format 1: "DECISION: APPROVE"
            if line_upper.startswith("DECISION:"):
                decision_value = line_upper.replace("DECISION:", "").strip()
                approved = decision_value == "APPROVE"
                break

            # Format 2: "### Decision" followed by "APPROVE" on next line
            if "DECISION" in line_upper and i + 1 < len(lines):
                next_line = lines[i + 1].strip().upper()
                if next_line == "APPROVE":
                    approved = True
                    break
                elif next_line == "CHANGES_REQUESTED":
                    approved = False
                    break

        # Clean up output for comment
        comment = (
            f"## Auto-Review by Reviewer Agent ({self.config.llm_backend})\n\n{output}"
        )

        return {"approved": approved, "comment": comment}

    def _load_accumulated_fixes(
        self, pr_number: int, issue_number: int | None = None
    ) -> dict[str, object]:
        """Load accumulated fixes for a PR."""
        fix_file = self.accumulated_fixes_dir / f"pr-{pr_number}.json"
        if fix_file.exists():
            with open(fix_file) as f:
                data: dict[str, object] = json.load(f)
            if issue_number and not data.get("issue_number"):
                data["issue_number"] = issue_number
            return data

        timestamp = datetime.now().isoformat()
        return {
            "pr_number": pr_number,
            "issue_number": issue_number,
            "created_at": timestamp,
            "last_updated": timestamp,
            "accumulated_issues": [],
            "threshold": self.ACCUMULATED_THRESHOLD,
            "current_count": 0,
        }

    def _save_accumulated_fixes(self, pr_number: int, data: dict) -> None:
        """Save accumulated fixes for a PR."""
        data["last_updated"] = datetime.now().isoformat()
        fix_file = self.accumulated_fixes_dir / f"pr-{pr_number}.json"
        with open(fix_file, "w") as f:
            json.dump(data, f, indent=2)

    def _add_accumulated_issue(
        self,
        pr_number: int,
        issue: dict,
        issue_number: int | None = None,
    ) -> bool:
        """
        Add a minor/trivial issue to accumulated fixes.

        Returns True if threshold reached, False otherwise.
        """
        data = self._load_accumulated_fixes(pr_number, issue_number=issue_number)
        entry = dict(issue)
        entry["review_id"] = f"review-{uuid.uuid4().hex[:8]}"
        entry["timestamp"] = datetime.now().isoformat()

        issues_list = cast(list, data["accumulated_issues"])
        issues_list.append(entry)
        data["current_count"] = len(issues_list)

        self._save_accumulated_fixes(pr_number, data)

        current_count = cast(int, data["current_count"])
        threshold = cast(int, data["threshold"])
        return current_count >= threshold

    def _format_accumulated_feedback(self, pr_number: int) -> str:
        """Format accumulated issues into feedback."""
        data = self._load_accumulated_fixes(pr_number)
        issues = cast(list, data.get("accumulated_issues", []))
        if not issues:
            return ""

        feedback = "## Accumulated Minor/Trivial Issues\n\n"
        feedback += f"Total issues: {len(issues)}\n\n"

        by_severity: dict[str, list[dict]] = {}
        for issue_obj in issues:
            issue = cast(dict, issue_obj)
            severity = issue.get("severity", "minor")
            by_severity.setdefault(severity, []).append(issue)

        for severity in ["minor", "trivial"]:
            entries = by_severity.get(severity, [])
            if not entries:
                continue

            feedback += f"### {severity.upper()} ({len(entries)})\n\n"
            for issue in entries:
                feedback += f"**{issue['file']}:{issue['line']}**\n"
                feedback += f"- {issue['description']}\n"
                feedback += f"- Suggestion: {issue['suggestion']}\n\n"

        return feedback

    def _clear_accumulated_fixes(self, pr_number: int) -> None:
        """Clear accumulated fixes after sending feedback."""
        fix_file = self.accumulated_fixes_dir / f"pr-{pr_number}.json"
        if fix_file.exists():
            fix_file.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reviewer Agent - Autonomous code review daemon"
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
        help="Review one PR and exit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--dry-run",
        nargs="?",
        const="simulate-all",
        choices=["execute-tests", "simulate-all"],
        help="Dry-run mode (default: simulate-all): execute-tests or simulate-all",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    agent = ReviewerAgent(args.repo, config_path=args.config, dry_run=args.dry_run)

    if args.once:
        success = agent.run_once()
        sys.exit(0 if success else 1)
    else:
        agent.run()


if __name__ == "__main__":
    main()
