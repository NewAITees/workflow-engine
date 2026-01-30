"""Lock mechanism for coordinating multiple agents."""

import logging
import time
import uuid
from dataclasses import dataclass

from .github_client import GitHubClient

logger = logging.getLogger(__name__)

# Lock timeout: 30 minutes (in milliseconds)
LOCK_TIMEOUT = 30 * 60 * 1000


@dataclass
class LockResult:
    """Result of a lock attempt."""

    success: bool
    lock_id: str | None = None
    error: str | None = None


class LockManager:
    """Manages distributed locks using GitHub comments and labels."""

    def __init__(
        self,
        github: GitHubClient,
        agent_type: str,
        agent_id: str | None = None,
    ):
        self.github = github
        self.agent_type = agent_type
        self.agent_id = agent_id or f"{agent_type}-{uuid.uuid4().hex[:8]}"
        self.lock_wait_time = 2.0  # seconds to wait for conflict detection

    def _create_ack_message(self, timestamp: int) -> str:
        """Create ACK message for locking."""
        return f"ACK:{self.agent_type}:{self.agent_id}:{timestamp}"

    def _parse_ack_message(self, message: str) -> dict | None:
        """Parse ACK message."""
        if not message.startswith("ACK:"):
            return None

        parts = message.split(":")
        if len(parts) != 4:
            return None

        try:
            return {
                "agent_type": parts[1],
                "agent_id": parts[2],
                "timestamp": int(parts[3]),
            }
        except (ValueError, TypeError):
            # Malformed timestamp
            return None

    def get_active_lock(self, issue_number: int) -> str | None:
        """
        Get the agent_id of the active lock holder (within LOCK_TIMEOUT).

        Returns:
            agent_id if there's an active lock, None otherwise
        """
        current_time = int(time.time() * 1000)
        min_valid_timestamp = current_time - LOCK_TIMEOUT

        comments = self.github.get_issue_comments(issue_number, limit=20)

        for comment in comments:
            parsed = self._parse_ack_message(comment.get("body", ""))
            if parsed and parsed["agent_type"] == self.agent_type:
                # Check if lock is still valid (within timeout)
                if parsed["timestamp"] >= min_valid_timestamp:
                    agent_id = str(parsed["agent_id"])
                    logger.debug(
                        f"Found active lock on issue #{issue_number}: "
                        f"agent={agent_id}, "
                        f"age={(current_time - parsed['timestamp']) / 1000:.1f}s"
                    )
                    return agent_id

        logger.debug(f"No active lock found on issue #{issue_number}")
        return None

    def try_lock_issue(
        self,
        issue_number: int,
        from_status: str,
        to_status: str,
    ) -> LockResult:
        """
        Try to acquire a lock on an issue.

        Uses ACK comment + label transition for distributed locking.
        Checks for existing active locks (within LOCK_TIMEOUT) before attempting.

        Args:
            issue_number: The issue to lock
            from_status: Expected current status label
            to_status: Target status label after lock

        Returns:
            LockResult indicating success or failure
        """
        # Check for existing active lock
        active_lock_holder = self.get_active_lock(issue_number)
        if active_lock_holder:
            if active_lock_holder == self.agent_id:
                # We already hold the lock, can proceed
                logger.debug(
                    f"Already holding lock on issue #{issue_number}, proceeding"
                )
            else:
                # Another agent holds the lock
                logger.info(
                    f"Issue #{issue_number} is locked by {active_lock_holder}, skipping"
                )
                return LockResult(
                    success=False,
                    error=f"Locked by {active_lock_holder} (within timeout)",
                )

        timestamp = int(time.time() * 1000)
        ack_msg = self._create_ack_message(timestamp)
        # Define time window for valid ACKs (only ACKs within last 30 seconds count)
        min_valid_timestamp = timestamp - 30000

        # Step 1: Post ACK comment
        logger.debug(f"Posting ACK for issue #{issue_number}")
        if not self.github.comment_issue(issue_number, ack_msg):
            return LockResult(success=False, error="Failed to post ACK comment")

        # Step 2: Wait for potential conflicts
        time.sleep(self.lock_wait_time)

        # Step 3: Check if we're the first ACK within the time window
        comments = self.github.get_issue_comments(issue_number, limit=10)
        ack_comments = []

        for comment in comments:
            parsed = self._parse_ack_message(comment.get("body", ""))
            if parsed and parsed["agent_type"] == self.agent_type:
                # Only consider ACKs within the valid time window
                if parsed["timestamp"] >= min_valid_timestamp:
                    ack_comments.append(parsed)

        # Sort by timestamp to find the winner (earliest wins)
        ack_comments.sort(key=lambda x: x["timestamp"])

        if not ack_comments:
            return LockResult(success=False, error="ACK comment not found")

        # Check if we're the first (winner) among recent ACKs
        if ack_comments[0]["agent_id"] != self.agent_id:
            logger.info(
                f"Lock conflict on issue #{issue_number}, "
                f"winner: {ack_comments[0]['agent_id']}"
            )
            return LockResult(
                success=False,
                error=f"Lost lock to {ack_comments[0]['agent_id']}",
            )

        # Step 4: Perform label transition
        logger.debug(
            f"Transitioning issue #{issue_number}: {from_status} -> {to_status}"
        )

        # Remove old label
        if not self.github.remove_label(issue_number, from_status):
            logger.warning(f"Failed to remove label {from_status}")
            # Continue anyway - label might already be removed

        # Add new label
        if not self.github.add_label(issue_number, to_status):
            return LockResult(success=False, error=f"Failed to add label {to_status}")

        # Step 5: Verify the transition
        issue = self.github.get_issue(issue_number)
        if issue is None:
            return LockResult(success=False, error="Failed to verify issue state")

        if to_status not in issue.labels:
            return LockResult(
                success=False,
                error=f"Label {to_status} not found after transition",
            )

        logger.info(f"Successfully locked issue #{issue_number}")
        return LockResult(success=True, lock_id=ack_msg)

    def try_lock_pr(
        self,
        pr_number: int,
        from_status: str,
        to_status: str,
    ) -> LockResult:
        """
        Try to acquire a lock on a pull request.

        Similar to issue locking but for PRs.
        Checks for existing active locks (within LOCK_TIMEOUT) before attempting.
        """
        # Check for existing active lock
        active_lock_holder = self.get_active_lock(pr_number)
        if active_lock_holder:
            if active_lock_holder == self.agent_id:
                logger.debug(f"Already holding lock on PR #{pr_number}, proceeding")
            else:
                logger.info(
                    f"PR #{pr_number} is locked by {active_lock_holder}, skipping"
                )
                return LockResult(
                    success=False,
                    error=f"Locked by {active_lock_holder} (within timeout)",
                )

        timestamp = int(time.time() * 1000)
        ack_msg = self._create_ack_message(timestamp)
        min_valid_timestamp = timestamp - 30000

        # Step 1: Post ACK comment
        logger.debug(f"Posting ACK for PR #{pr_number}")
        if not self.github.comment_pr(pr_number, ack_msg):
            return LockResult(success=False, error="Failed to post ACK comment")

        # Step 2: Wait for potential conflicts
        time.sleep(self.lock_wait_time)

        # Step 3: For PRs, we use the same comment checking via issue API
        # (PRs are issues in GitHub API)
        comments = self.github.get_issue_comments(pr_number, limit=10)
        ack_comments = []

        for comment in comments:
            parsed = self._parse_ack_message(comment.get("body", ""))
            if parsed and parsed["agent_type"] == self.agent_type:
                # Only consider ACKs within the valid time window
                if parsed["timestamp"] >= min_valid_timestamp:
                    ack_comments.append(parsed)

        ack_comments.sort(key=lambda x: x["timestamp"])

        if not ack_comments or ack_comments[0]["agent_id"] != self.agent_id:
            return LockResult(success=False, error="Lost lock to another agent")

        # Step 4: Label transition
        if not self.github.remove_pr_label(pr_number, from_status):
            logger.warning(f"Failed to remove PR label {from_status}")

        if not self.github.add_pr_label(pr_number, to_status):
            return LockResult(
                success=False, error=f"Failed to add PR label {to_status}"
            )

        logger.info(f"Successfully locked PR #{pr_number}")
        return LockResult(success=True, lock_id=ack_msg)

    def mark_failed(
        self,
        issue_number: int,
        current_status: str,
        error_message: str,
    ) -> bool:
        """Mark an issue as failed with error details."""
        # Remove current status
        self.github.remove_label(issue_number, current_status)

        # Add failed status
        self.github.add_label(issue_number, "status:failed")

        # Comment with error
        comment = f"❌ **Processing failed**\n\n```\n{error_message}\n```"
        return self.github.comment_issue(issue_number, comment)

    def mark_needs_clarification(
        self,
        issue_number: int,
        current_status: str,
        reason: str,
    ) -> None:
        """
        Mark an issue as needing clarification.

        Currently logs the event; label transition handled by caller.
        """
        logger.warning(
            f"Issue #{issue_number} needs clarification (status: {current_status}): {reason}"
        )
