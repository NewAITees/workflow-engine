#!/usr/bin/env python3
"""
Status Dashboard - View workflow status for a repository.

Displays Issues and PRs grouped by workflow status labels.

Usage:
    uv run scripts/status.py <owner/repo>
    uv run scripts/status.py <owner/repo> --json
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.github_client import GitHubClient, Issue, PullRequest  # noqa: E402


@dataclass
class StatusSummary:
    """Summary of workflow status."""

    ready: list[Issue]
    implementing: list[Issue]
    reviewing: list[PullRequest]
    in_review: list[PullRequest]
    approved: list[PullRequest]
    changes_requested: list[PullRequest]
    failed: list[Issue | PullRequest]


class StatusDashboard:
    """Dashboard for viewing workflow status."""

    # Status labels
    LABELS = {
        "ready": "status:ready",
        "implementing": "status:implementing",
        "reviewing": "status:reviewing",
        "in_review": "status:in-review",
        "approved": "status:approved",
        "changes_requested": "status:changes-requested",
        "failed": "status:failed",
    }

    def __init__(self, repo: str, gh_cli: str = "gh"):
        self.repo = repo
        self.github = GitHubClient(repo, gh_cli=gh_cli)

    def get_status(self) -> StatusSummary:
        """Fetch current workflow status."""
        return StatusSummary(
            ready=self.github.list_issues(labels=[self.LABELS["ready"]]),
            implementing=self.github.list_issues(labels=[self.LABELS["implementing"]]),
            reviewing=self.github.list_prs(labels=[self.LABELS["reviewing"]]),
            in_review=self.github.list_prs(labels=[self.LABELS["in_review"]]),
            approved=self.github.list_prs(labels=[self.LABELS["approved"]]),
            changes_requested=self.github.list_prs(
                labels=[self.LABELS["changes_requested"]]
            ),
            failed=self.github.list_issues(labels=[self.LABELS["failed"]])
            + self.github.list_prs(labels=[self.LABELS["failed"]]),
        )

    def print_status(self, summary: StatusSummary) -> None:
        """Print status in human-readable format."""
        print("=" * 60)
        print(f"  Workflow Status Dashboard: {self.repo}")
        print("=" * 60)
        print()

        # Issues section
        self._print_section(
            "Issues Ready for Implementation",
            summary.ready,
            "status:ready",
            icon="📋",
        )

        self._print_section(
            "Issues Being Implemented",
            summary.implementing,
            "status:implementing",
            icon="🔨",
        )

        # PRs section
        self._print_section(
            "PRs Waiting for Review",
            summary.reviewing,
            "status:reviewing",
            icon="👀",
        )

        self._print_section(
            "PRs Under Review",
            summary.in_review,
            "status:in-review",
            icon="🔍",
        )

        self._print_section(
            "Approved PRs",
            summary.approved,
            "status:approved",
            icon="✅",
        )

        self._print_section(
            "PRs with Requested Changes",
            summary.changes_requested,
            "status:changes-requested",
            icon="📝",
        )

        # Failed section
        if summary.failed:
            self._print_section(
                "Failed Items",
                summary.failed,
                "status:failed",
                icon="❌",
            )

        # Summary counts
        print("-" * 60)
        print("Summary:")
        print(
            f"  Issues:  {len(summary.ready)} ready, {len(summary.implementing)} in progress"
        )
        total_prs = (
            len(summary.reviewing)
            + len(summary.in_review)
            + len(summary.approved)
            + len(summary.changes_requested)
        )
        print(f"  PRs:     {total_prs} total ({len(summary.approved)} approved)")
        if summary.failed:
            print(f"  Failed:  {len(summary.failed)} items need attention")
        print()

    def _print_section(
        self,
        title: str,
        items: list[Issue | PullRequest],
        label: str,
        icon: str = "",
    ) -> None:
        """Print a section of items."""
        print(f"{icon} {title} [{label}]")
        print("-" * 60)

        if not items:
            print("  (none)")
        else:
            for item in items:
                number = item.number
                item_title = (
                    item.title[:50] + "..." if len(item.title) > 50 else item.title
                )
                item_type = "PR" if isinstance(item, PullRequest) else "Issue"
                print(f"  #{number:4d} [{item_type}] {item_title}")

        print()

    def to_json(self, summary: StatusSummary) -> str:
        """Convert status to JSON."""

        def serialize_item(item: Issue | PullRequest) -> dict:
            data = {
                "number": item.number,
                "title": item.title,
                "labels": item.labels,
                "type": "pr" if isinstance(item, PullRequest) else "issue",
            }
            if isinstance(item, PullRequest):
                data["head_ref"] = item.head_ref
                data["base_ref"] = item.base_ref
            return data

        output = {
            "repository": self.repo,
            "status": {
                "ready": [serialize_item(i) for i in summary.ready],
                "implementing": [serialize_item(i) for i in summary.implementing],
                "reviewing": [serialize_item(i) for i in summary.reviewing],
                "in_review": [serialize_item(i) for i in summary.in_review],
                "approved": [serialize_item(i) for i in summary.approved],
                "changes_requested": [
                    serialize_item(i) for i in summary.changes_requested
                ],
                "failed": [serialize_item(i) for i in summary.failed],
            },
            "counts": {
                "issues_ready": len(summary.ready),
                "issues_implementing": len(summary.implementing),
                "prs_reviewing": len(summary.reviewing),
                "prs_in_review": len(summary.in_review),
                "prs_approved": len(summary.approved),
                "prs_changes_requested": len(summary.changes_requested),
                "failed": len(summary.failed),
            },
        }
        return json.dumps(output, indent=2, ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="View workflow status for a repository"
    )
    parser.add_argument(
        "repo",
        help="Repository in owner/repo format",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    parser.add_argument(
        "--gh-cli",
        default="gh",
        help="Path to gh CLI (default: gh)",
    )

    args = parser.parse_args()

    dashboard = StatusDashboard(args.repo, gh_cli=args.gh_cli)

    try:
        summary = dashboard.get_status()

        if args.json:
            print(dashboard.to_json(summary))
        else:
            dashboard.print_status(summary)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
