"""GitHub API client using gh CLI."""

import json
import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Issue:
    """GitHub Issue representation."""

    number: int
    title: str
    body: str
    labels: list[str]
    state: str = "open"


@dataclass
class PullRequest:
    """GitHub Pull Request representation."""

    number: int
    title: str
    body: str
    labels: list[str]
    head_ref: str
    base_ref: str
    state: str = "open"


class GitHubClient:
    """GitHub operations via gh CLI."""

    def __init__(self, repo: str, gh_cli: str = "gh"):
        self.repo = repo
        self.gh = gh_cli

    def _run(
        self, args: list[str], check: bool = True, capture: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run gh CLI command."""
        cmd = [self.gh] + args
        logger.debug(f"Running: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=check,
            encoding="utf-8",
        )

        if result.returncode != 0 and not check:
            logger.warning(f"Command failed: {result.stderr}")

        return result

    # ========== Repository Operations ==========

    def get_default_branch(self) -> str:
        """Get the default branch name for the repository."""
        args = [
            "api",
            f"/repos/{self.repo}",
            "--jq",
            ".default_branch",
        ]
        result = self._run(args, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        # Fallback
        return "main"

    # ========== Issue Operations ==========

    def list_issues(
        self,
        labels: list[str] | None = None,
        state: str = "open",
        limit: int = 30,
    ) -> list[Issue]:
        """List issues with optional label filter."""
        args = [
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            state,
            "--json",
            "number,title,body,labels,state",
            "--limit",
            str(limit),
        ]

        if labels:
            for label in labels:
                args.extend(["--label", label])

        result = self._run(args)
        data = json.loads(result.stdout) if result.stdout else []

        return [
            Issue(
                number=item["number"],
                title=item["title"],
                body=item["body"] or "",
                labels=[lbl["name"] for lbl in item.get("labels", [])],
                state=item.get("state", "open"),
            )
            for item in data
        ]

    def get_issue(self, number: int) -> Issue | None:
        """Get a specific issue."""
        args = [
            "issue",
            "view",
            str(number),
            "--repo",
            self.repo,
            "--json",
            "number,title,body,labels,state",
        ]

        result = self._run(args, check=False)
        if result.returncode != 0:
            return None

        item = json.loads(result.stdout)
        return Issue(
            number=item["number"],
            title=item["title"],
            body=item["body"] or "",
            labels=[lbl["name"] for lbl in item.get("labels", [])],
            state=item.get("state", "open"),
        )

    def add_label(self, issue_number: int, label: str) -> bool:
        """Add a label to an issue."""
        args = [
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self.repo,
            "--add-label",
            label,
        ]
        result = self._run(args, check=False)
        return result.returncode == 0

    def remove_label(self, issue_number: int, label: str) -> bool:
        """Remove a label from an issue."""
        args = [
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self.repo,
            "--remove-label",
            label,
        ]
        result = self._run(args, check=False)
        return result.returncode == 0

    def comment_issue(self, issue_number: int, body: str) -> bool:
        """Add a comment to an issue."""
        args = [
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            self.repo,
            "--body",
            body,
        ]
        result = self._run(args, check=False)
        return result.returncode == 0

    def update_issue_body(self, issue_number: int, body: str) -> bool:
        """Update issue body."""
        args = [
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self.repo,
            "--body",
            body,
        ]
        result = self._run(args, check=False)
        return result.returncode == 0

    def get_issue_comments(self, issue_number: int, limit: int = 30) -> list[dict]:
        """Get comments on an issue."""
        args = [
            "api",
            f"/repos/{self.repo}/issues/{issue_number}/comments",
            "--jq",
            f".[-{limit}:] | .[] | {{id: .id, body: .body, created_at: .created_at}}",
        ]
        result = self._run(args, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return []

        comments = []
        for line in result.stdout.strip().split("\n"):
            if line:
                try:
                    comments.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return comments

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> int | None:
        """Create a new issue."""
        args = [
            "issue",
            "create",
            "--repo",
            self.repo,
            "--title",
            title,
            "--body",
            body,
        ]

        if labels:
            for label in labels:
                args.extend(["--label", label])

        result = self._run(args, check=False)
        if result.returncode != 0:
            return None

        # Parse issue number from URL
        url = result.stdout.strip()
        try:
            return int(url.split("/")[-1])
        except (ValueError, IndexError):
            return None

    # ========== Pull Request Operations ==========

    def list_prs(
        self,
        labels: list[str] | None = None,
        state: str = "open",
        limit: int = 30,
    ) -> list[PullRequest]:
        """List pull requests with optional label filter."""
        args = [
            "pr",
            "list",
            "--repo",
            self.repo,
            "--state",
            state,
            "--json",
            "number,title,body,labels,headRefName,baseRefName,state",
            "--limit",
            str(limit),
        ]

        if labels:
            for label in labels:
                args.extend(["--label", label])

        result = self._run(args)
        data = json.loads(result.stdout) if result.stdout else []

        return [
            PullRequest(
                number=item["number"],
                title=item["title"],
                body=item["body"] or "",
                labels=[lbl["name"] for lbl in item.get("labels", [])],
                head_ref=item["headRefName"],
                base_ref=item["baseRefName"],
                state=item.get("state", "open"),
            )
            for item in data
        ]

    def get_pr(self, number: int) -> PullRequest | None:
        """Get a specific pull request."""
        args = [
            "pr",
            "view",
            str(number),
            "--repo",
            self.repo,
            "--json",
            "number,title,body,labels,headRefName,baseRefName,state",
        ]

        result = self._run(args, check=False)
        if result.returncode != 0:
            return None

        item = json.loads(result.stdout)
        return PullRequest(
            number=item["number"],
            title=item["title"],
            body=item["body"] or "",
            labels=[lbl["name"] for lbl in item.get("labels", [])],
            head_ref=item["headRefName"],
            base_ref=item["baseRefName"],
            state=item.get("state", "open"),
        )

    def get_pr_diff(self, number: int) -> str:
        """Get the diff of a pull request."""
        args = ["pr", "diff", str(number), "--repo", self.repo]
        result = self._run(args, check=False)
        return result.stdout if result.returncode == 0 else ""

    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        labels: list[str] | None = None,
    ) -> str | None:
        """Create a pull request."""
        args = [
            "pr",
            "create",
            "--repo",
            self.repo,
            "--title",
            title,
            "--body",
            body,
            "--head",
            head,
            "--base",
            base,
        ]

        if labels:
            for label in labels:
                args.extend(["--label", label])

        result = self._run(args, check=False)
        if result.returncode != 0:
            logger.error(f"Failed to create PR: {result.stderr}")
            return None

        return result.stdout.strip()  # Returns PR URL

    def add_pr_label(self, pr_number: int, label: str) -> bool:
        """Add a label to a pull request."""
        args = [
            "pr",
            "edit",
            str(pr_number),
            "--repo",
            self.repo,
            "--add-label",
            label,
        ]
        result = self._run(args, check=False)
        return result.returncode == 0

    def remove_pr_label(self, pr_number: int, label: str) -> bool:
        """Remove a label from a pull request."""
        args = [
            "pr",
            "edit",
            str(pr_number),
            "--repo",
            self.repo,
            "--remove-label",
            label,
        ]
        result = self._run(args, check=False)
        return result.returncode == 0

    def comment_pr(self, pr_number: int, body: str) -> bool:
        """Add a comment to a pull request."""
        args = [
            "pr",
            "comment",
            str(pr_number),
            "--repo",
            self.repo,
            "--body",
            body,
        ]
        result = self._run(args, check=False)
        return result.returncode == 0

    def approve_pr(self, pr_number: int, body: str = "LGTM") -> bool:
        """Approve a pull request."""
        args = [
            "pr",
            "review",
            str(pr_number),
            "--repo",
            self.repo,
            "--approve",
            "--body",
            body,
        ]
        result = self._run(args, check=False)
        return result.returncode == 0

    def request_changes_pr(self, pr_number: int, body: str) -> bool:
        """Request changes on a pull request."""
        args = [
            "pr",
            "review",
            str(pr_number),
            "--repo",
            self.repo,
            "--request-changes",
            "--body",
            body,
        ]
        result = self._run(args, check=False)
        return result.returncode == 0

    def merge_pr(self, pr_number: int, method: str = "squash") -> bool:
        """Merge a pull request."""
        args = [
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            self.repo,
            f"--{method}",
            "--delete-branch",
        ]
        result = self._run(args, check=False)
        return result.returncode == 0

    def get_pr_checks(self, pr_number: int) -> dict:
        """Get CI check status for a PR."""
        args = [
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            self.repo,
            "--json",
            "name,state",
        ]
        result = self._run(args, check=False)
        if result.returncode != 0:
            return {"checks": [], "all_passed": True}  # No CI means pass

        checks = json.loads(result.stdout) if result.stdout else []

        # If no checks, consider it passed (no CI configured)
        if not checks:
            return {"checks": [], "all_passed": True}

        # Check if all checks have passed (case-insensitive)
        all_passed = all(c.get("state", "").upper() == "SUCCESS" for c in checks)

        return {"checks": checks, "all_passed": all_passed}

    def is_ci_green(self, pr_number: int) -> bool:
        """Check if all CI checks have passed."""
        result = self.get_pr_checks(pr_number)
        return bool(result["all_passed"])

    def get_ci_status(self, pr_number: int) -> dict:
        """
        Get detailed CI check status for PR.

        Returns:
            {
                'status': 'pending' | 'success' | 'failure' | 'none',
                'conclusion': overall conclusion,
                'checks': [list of check results with details],
                'pending_count': number of pending checks,
                'failed_count': number of failed checks
            }
        """
        # Use GitHub API to get check runs with more details
        args = [
            "api",
            f"/repos/{self.repo}/commits/pulls/{pr_number}/check-runs",
            "--jq",
            ".check_runs[] | {name: .name, status: .status, conclusion: .conclusion, "
            "started_at: .started_at, completed_at: .completed_at, html_url: .html_url}",
        ]

        result = self._run(args, check=False)

        if result.returncode != 0 or not result.stdout.strip():
            # Fallback to simple checks
            basic_result = self.get_pr_checks(pr_number)
            if not basic_result["checks"]:
                return {
                    "status": "none",
                    "conclusion": "none",
                    "checks": [],
                    "pending_count": 0,
                    "failed_count": 0,
                }

            all_passed = basic_result["all_passed"]
            return {
                "status": "success" if all_passed else "failure",
                "conclusion": "success" if all_passed else "failure",
                "checks": basic_result["checks"],
                "pending_count": 0,
                "failed_count": 0 if all_passed else len(basic_result["checks"]),
            }

        # Parse detailed check runs
        checks = []
        for line in result.stdout.strip().split("\n"):
            if line:
                try:
                    checks.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        if not checks:
            return {
                "status": "none",
                "conclusion": "none",
                "checks": [],
                "pending_count": 0,
                "failed_count": 0,
            }

        # Analyze check status
        pending_count = sum(1 for c in checks if c.get("status") == "in_progress")
        failed_count = sum(
            1
            for c in checks
            if c.get("conclusion") in ["failure", "timed_out", "action_required"]
        )
        completed_count = sum(1 for c in checks if c.get("status") == "completed")

        # Determine overall status
        if pending_count > 0:
            status = "pending"
            conclusion = "pending"
        elif failed_count > 0:
            status = "failure"
            conclusion = "failure"
        elif completed_count == len(checks):
            status = "success"
            conclusion = "success"
        else:
            status = "pending"
            conclusion = "pending"

        return {
            "status": status,
            "conclusion": conclusion,
            "checks": checks,
            "pending_count": pending_count,
            "failed_count": failed_count,
        }

    def get_ci_logs(self, pr_number: int) -> list[dict]:
        """
        Get CI failure logs for failed checks.

        Returns:
            List of failed checks with their logs:
            [
                {
                    'name': check name,
                    'conclusion': failure reason,
                    'html_url': link to logs,
                    'output': {
                        'title': error title,
                        'summary': error summary
                    }
                }
            ]
        """
        # Get detailed check runs with output
        args = [
            "api",
            f"/repos/{self.repo}/commits/pulls/{pr_number}/check-runs",
            "--jq",
            '.check_runs[] | select(.conclusion == "failure" or .conclusion == "timed_out") '
            "| {name: .name, conclusion: .conclusion, html_url: .html_url, "
            "output: {title: .output.title, summary: .output.summary}}",
        ]

        result = self._run(args, check=False)

        if result.returncode != 0 or not result.stdout.strip():
            logger.warning(f"Could not fetch CI logs for PR #{pr_number}")
            return []

        failed_checks = []
        for line in result.stdout.strip().split("\n"):
            if line:
                try:
                    failed_checks.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        return failed_checks

    def get_pr_reviews(self, pr_number: int) -> list[dict]:
        """Get reviews for a pull request."""
        args = [
            "api",
            f"/repos/{self.repo}/pulls/{pr_number}/reviews",
            "--jq",
            ".[] | {id: .id, state: .state, body: .body, submitted_at: .submitted_at}",
        ]
        result = self._run(args, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return []

        reviews = []
        for line in result.stdout.strip().split("\n"):
            if line:
                try:
                    reviews.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return reviews
