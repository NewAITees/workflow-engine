#!/usr/bin/env python3
"""Planner Agent - Interactive specification generator.

Converts user stories into detailed specifications and creates
GitHub issues for the Worker Agent to implement.
"""

import argparse
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import get_agent_config
from shared.github_client import GitHubClient, Issue
from shared.llm_client import LLMClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("planner-agent")


class PlannerAgent:
    """Interactive agent for creating specifications from stories."""

    STATUS_READY = "status:ready"
    STATUS_ESCALATED = "status:escalated"
    STATUS_FAILED = "status:failed"
    MAX_ESCALATION_RETRIES = 3

    def __init__(self, repo: str, config_path: str | None = None):
        self.repo = repo
        self.config = get_agent_config(repo, config_path)
        self.github = GitHubClient(repo, gh_cli=self.config.gh_cli)
        self.llm = LLMClient(self.config)

        logger.info(f"Planner Agent initialized for {repo}")
        logger.info(f"LLM backend: {self.config.llm_backend}")

    def interactive_mode(self) -> None:
        """Run in interactive mode, converting stories to specs."""
        print(f"\n{'=' * 60}")
        print("Planner Agent - Interactive Mode")
        print(f"Repository: {self.repo}")
        print(f"LLM Backend: {self.config.llm_backend}")
        print(f"{'=' * 60}")
        print("\nEnter a user story to convert to a specification.")
        print("Type 'quit' or 'exit' to stop.\n")

        while True:
            try:
                print("-" * 40)
                story = self._get_multiline_input("📝 Story")

                if story.lower() in ("quit", "exit", "q"):
                    print("\nGoodbye!")
                    break

                if not story.strip():
                    print("Empty story, please try again.")
                    continue

                # Generate specification
                print(
                    f"\n🤔 Generating specification with {self.config.llm_backend}..."
                )
                spec = self._generate_spec(story)

                if not spec:
                    print("❌ Failed to generate specification")
                    continue

                # Display spec
                print("\n" + "=" * 40)
                print("📋 Generated Specification:")
                print("=" * 40)
                print(spec)
                print("=" * 40)

                # Confirm
                confirm = (
                    input("\n✅ Create issue with this spec? (y/n): ").strip().lower()
                )

                if confirm in ("y", "yes"):
                    title = self._extract_title(story, spec)
                    issue_num = self.github.create_issue(
                        title=title,
                        body=spec,
                        labels=[self.STATUS_READY],
                    )

                    if issue_num:
                        print(f"\n✅ Issue #{issue_num} created!")
                        print(f"   https://github.com/{self.repo}/issues/{issue_num}")
                    else:
                        print("\n❌ Failed to create issue")
                else:
                    print("\n❌ Cancelled")

            except KeyboardInterrupt:
                print("\n\nGoodbye!")
                break
            except Exception as e:
                logger.exception(f"Error: {e}")
                print(f"\n❌ Error: {e}")

    def create_spec(self, story: str) -> str | None:
        """Create a specification from a story (non-interactive)."""
        spec = self._generate_spec(story)
        if not spec:
            return None

        title = self._extract_title(story, spec)
        issue_num = self.github.create_issue(
            title=title,
            body=spec,
            labels=[self.STATUS_READY],
        )

        if issue_num:
            return f"Issue #{issue_num} created: https://github.com/{self.repo}/issues/{issue_num}"
        return None

    def run_once(self) -> bool:
        """Process one escalated issue and return True if handled."""
        return self._process_escalations(limit=1)

    def run_daemon(self) -> None:
        """Run escalation processing loop."""
        logger.info(f"Starting Planner escalation loop for {self.repo}")
        logger.info(f"Poll interval: {self.config.poll_interval}s")
        while True:
            try:
                self._process_escalations(limit=20)
                time.sleep(self.config.poll_interval)
            except KeyboardInterrupt:
                logger.info("Shutting down Planner loop")
                break
            except Exception as e:
                logger.exception(f"Unexpected planner loop error: {e}")
                time.sleep(60)

    def _process_escalations(self, limit: int = 20) -> bool:
        """
        Detect escalations from comments and regenerate issue specification.

        Returns True if at least one issue was processed.
        """
        issues = self.github.list_issues(state="open", limit=100)
        processed = 0

        for issue in issues:
            if processed >= limit:
                break
            if self._try_process_escalated_issue(issue.number):
                processed += 1

        if processed:
            logger.info(f"Processed {processed} escalated issue(s)")
        return processed > 0

    def _try_process_escalated_issue(self, issue_number: int) -> bool:
        """Process a single escalated issue if a new escalation exists."""
        issue = self.github.get_issue(issue_number)
        if issue is None:
            return False

        comments = self.github.get_issue_comments(issue_number, limit=100)
        escalation = self._latest_escalation(comments)
        if escalation is None and self.STATUS_FAILED in issue.labels:
            # Auto-bridge failed issues into escalation flow so Planner can retry.
            if self._create_failed_issue_escalation(issue, comments):
                comments = self.github.get_issue_comments(issue_number, limit=100)
                escalation = self._latest_escalation(comments)
        if escalation is None:
            return False

        retry_count, retry_ts = self._latest_planner_retry(comments)
        escalation_ts = self._parse_timestamp(escalation.get("created_at"))
        if (
            retry_ts is not None
            and escalation_ts is not None
            and escalation_ts <= retry_ts
        ):
            return False

        if self.STATUS_ESCALATED not in issue.labels:
            self.github.add_label(issue.number, self.STATUS_ESCALATED)

        if retry_count >= self.MAX_ESCALATION_RETRIES:
            self.github.remove_label(issue.number, self.STATUS_ESCALATED)
            self.github.add_label(issue.number, self.STATUS_FAILED)
            self.github.comment_issue(
                issue.number,
                f"⚠️ Planner retry limit reached.\n\n"
                f"PLANNER_RETRY:{retry_count}\n\n"
                f"Issue marked as `{self.STATUS_FAILED}` for manual intervention.",
            )
            return True

        feedback = self._collect_escalation_feedback(comments)
        prompt = (
            "Refine the following technical specification using the escalation feedback.\n\n"
            "Return a complete revised specification in markdown.\n\n"
            f"## Current Specification\n{issue.body}\n\n"
            f"## Escalation Feedback\n{feedback}\n"
        )
        revised_spec = self._generate_spec(prompt)
        if not revised_spec:
            self.github.comment_issue(
                issue.number,
                "⚠️ Planner failed to regenerate specification automatically.",
            )
            return False

        if not self.github.update_issue_body(issue.number, revised_spec):
            self.github.comment_issue(
                issue.number,
                "⚠️ Planner generated a revised specification but failed to update the issue body.",
            )
            return False

        new_retry = retry_count + 1
        self.github.comment_issue(
            issue.number,
            f"✅ Planner updated the specification from escalation feedback.\n\n"
            f"PLANNER_RETRY:{new_retry}\n\n"
            f"Transitioning issue back to `{self.STATUS_READY}`.",
        )
        self.github.remove_label(issue.number, self.STATUS_ESCALATED)
        self.github.remove_label(issue.number, self.STATUS_FAILED)
        self.github.add_label(issue.number, self.STATUS_READY)
        return True

    def _latest_escalation(self, comments: list[dict]) -> dict | None:
        """Return latest escalation comment if exists."""
        latest: dict | None = None
        latest_ts: datetime | None = None
        pattern = re.compile(r"ESCALATION:(worker|reviewer)", re.IGNORECASE)

        for comment in comments:
            body = str(comment.get("body", ""))
            if not pattern.search(body):
                continue
            ts = self._parse_timestamp(comment.get("created_at"))
            if latest is None or (
                ts is not None and (latest_ts is None or ts > latest_ts)
            ):
                latest = comment
                latest_ts = ts
        return latest

    def _latest_planner_retry(
        self, comments: list[dict]
    ) -> tuple[int, datetime | None]:
        """Return max retry count and timestamp of latest retry marker."""
        max_retry = 0
        latest_ts: datetime | None = None
        pattern = re.compile(r"PLANNER_RETRY:(\d+)")

        for comment in comments:
            body = str(comment.get("body", ""))
            match = pattern.search(body)
            if not match:
                continue
            count = int(match.group(1))
            max_retry = max(max_retry, count)
            ts = self._parse_timestamp(comment.get("created_at"))
            if ts is not None and (latest_ts is None or ts > latest_ts):
                latest_ts = ts

        return max_retry, latest_ts

    def _collect_escalation_feedback(self, comments: list[dict]) -> str:
        """Collect escalation comment bodies for planner input."""
        pattern = re.compile(r"ESCALATION:(worker|reviewer)", re.IGNORECASE)
        parts = []
        for comment in comments:
            body = str(comment.get("body", "")).strip()
            if pattern.search(body):
                parts.append(body)
        return "\n\n---\n\n".join(parts[-5:]) if parts else "No escalation feedback."

    def _create_failed_issue_escalation(
        self, issue: Issue, comments: list[dict]
    ) -> bool:
        """
        Add a synthetic worker escalation marker for failed issues.

        This allows Planner to resume failed items without requiring manual
        comment edits.
        """
        latest_failure = self._latest_failure_detail(comments)
        body = (
            "ESCALATION:worker\n\n"
            "Reason: auto-bridge-from-status-failed\n\n"
            "Planner detected `status:failed` without escalation marker.\n\n"
            f"{latest_failure}"
        )
        return self.github.comment_issue(issue.number, body)

    def _latest_failure_detail(self, comments: list[dict]) -> str:
        """Extract latest failure context from issue comments."""
        for comment in reversed(comments):
            body = str(comment.get("body", "")).strip()
            if "❌ **Processing failed**" in body or "Processing failed" in body:
                return f"Latest failure context:\n{body[:2000]}"
        return "Latest failure context: unavailable."

    def _parse_timestamp(self, ts: str | None) -> datetime | None:
        """Parse GitHub timestamp safely."""
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _get_multiline_input(self, prompt: str) -> str:
        """Get potentially multiline input from user."""
        print(f"{prompt} (press Enter twice to finish):")
        lines = []
        empty_count = 0

        while True:
            try:
                line = input()
                if not line:
                    empty_count += 1
                    if empty_count >= 2:
                        break
                    lines.append("")
                else:
                    empty_count = 0
                    lines.append(line)
            except EOFError:
                break

        return "\n".join(lines).strip()

    def _generate_spec(self, story: str) -> str | None:
        """Generate a specification from a user story using LLM."""
        result = self.llm.create_spec(story)

        if not result.success:
            logger.error(f"LLM failed: {result.error}")
            return None

        return result.output.strip()

    def _extract_title(self, story: str, spec: str) -> str:
        """Extract a title from the story or spec."""
        # Try to get first line of story
        first_line = story.split("\n")[0].strip()

        # Clean up
        if first_line.startswith(("#", "-", "*")):
            first_line = first_line.lstrip("#-* ")

        # Truncate if too long
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."

        return first_line or "New Feature"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Planner Agent - Interactive specification generator"
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
        "--story",
        "-s",
        help="Story to convert (non-interactive mode)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one escalated issue and exit",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run planner escalation loop",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    agent = PlannerAgent(args.repo, config_path=args.config)

    if args.story:
        # Non-interactive mode
        result = agent.create_spec(args.story)
        if result:
            print(result)
            sys.exit(0)
        else:
            print("Failed to create specification")
            sys.exit(1)
    elif args.once:
        handled = agent.run_once()
        sys.exit(0 if handled else 1)
    elif args.daemon:
        agent.run_daemon()
    else:
        # Interactive mode
        agent.interactive_mode()


if __name__ == "__main__":
    main()
