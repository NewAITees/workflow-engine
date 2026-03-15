"""Shared helpers for WorkerAgent flows."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.github_client import Issue

logger = logging.getLogger("worker-agent")


def get_stale_timeout_minutes(agent: Any) -> int:
    """Get stale lock timeout from config with safe fallback."""
    value = getattr(agent.config, "stale_lock_timeout_minutes", 30)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = 30
    return timeout if timeout > 0 else 30


def parse_github_datetime(agent: Any, ts: str | None) -> datetime | None:
    """Parse GitHub timestamp string into timezone-aware datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_lock_reference_time(agent: Any, number: int) -> datetime | None:
    """
    Get reference timestamp for lock staleness.

    Priority:
    1. Most recent worker ACK comment timestamp
    2. Most recent comment timestamp
    """
    comments = agent.github.get_issue_comments(number, limit=50)
    latest_comment_time: datetime | None = None
    latest_ack_time: datetime | None = None

    for comment in comments:
        created = agent._parse_github_datetime(comment.get("created_at"))
        if created is None:
            continue

        if latest_comment_time is None or created > latest_comment_time:
            latest_comment_time = created

        body = str(comment.get("body", ""))
        if body.startswith("ACK:worker:"):
            if latest_ack_time is None or created > latest_ack_time:
                latest_ack_time = created

    return latest_ack_time or latest_comment_time


def is_stale_lock(agent: Any, number: int) -> tuple[bool, float | None]:
    """Check if implementing lock is stale based on reference timestamp."""
    ref_time = agent._get_lock_reference_time(number)
    if ref_time is None:
        return False, None

    age_minutes = (datetime.now(UTC) - ref_time).total_seconds() / 60.0
    return age_minutes > agent._get_stale_timeout_minutes(), age_minutes


def get_pr_recovery_status(agent: Any, pr_number: int) -> str:
    """
    Determine status to restore after stale recovery.

    If a CHANGES_REQUESTED review exists, restore to changes-requested.
    Otherwise restore to reviewing.
    """
    reviews = agent.github.get_pr_reviews(pr_number)
    for review in reversed(reviews):
        if review.get("state") == "CHANGES_REQUESTED":
            return str(agent.STATUS_CHANGES_REQUESTED)
    return str(agent.STATUS_REVIEWING)


def process_stale_locks(agent: Any) -> bool:
    """Recover stale locks from issues/PRs stuck in implementing."""
    recovered = False
    timeout = agent._get_stale_timeout_minutes()

    issues = agent.github.list_issues(labels=[agent.STATUS_IMPLEMENTING])
    for issue in issues:
        is_stale, age_minutes = agent._is_stale_lock(issue.number)
        if not is_stale:
            continue

        age_text = (
            f"{age_minutes:.1f} minutes" if age_minutes is not None else "unknown"
        )
        logger.warning(
            f"[{agent.agent_id}] Recovering stale issue lock #{issue.number} "
            f"(age={age_text}, timeout={timeout}m)"
        )
        agent.github.remove_label(issue.number, agent.STATUS_IMPLEMENTING)
        agent.github.add_label(issue.number, agent.STATUS_READY)
        agent.github.comment_issue(
            issue.number,
            f"⚠️ **Recovered stale lock**\n\n"
            f"- Previous status: `{agent.STATUS_IMPLEMENTING}`\n"
            f"- New status: `{agent.STATUS_READY}`\n"
            f"- Lock age: {age_text}\n"
            f"- Timeout: {timeout} minutes",
        )
        recovered = True

    prs = agent.github.list_prs(labels=[agent.STATUS_IMPLEMENTING])
    for pr in prs:
        is_stale, age_minutes = agent._is_stale_lock(pr.number)
        if not is_stale:
            continue

        target_status = agent._get_pr_recovery_status(pr.number)
        age_text = (
            f"{age_minutes:.1f} minutes" if age_minutes is not None else "unknown"
        )
        logger.warning(
            f"[{agent.agent_id}] Recovering stale PR lock #{pr.number} "
            f"(age={age_text}, timeout={timeout}m) -> {target_status}"
        )
        agent.github.remove_pr_label(pr.number, agent.STATUS_IMPLEMENTING)
        agent.github.add_pr_label(pr.number, target_status)
        agent.github.comment_pr(
            pr.number,
            f"⚠️ **Recovered stale lock**\n\n"
            f"- Previous status: `{agent.STATUS_IMPLEMENTING}`\n"
            f"- New status: `{target_status}`\n"
            f"- Lock age: {age_text}\n"
            f"- Timeout: {timeout} minutes",
        )
        recovered = True

    return recovered


def is_specification_unclear(agent: Any, failure_reason: str, spec: str) -> bool:
    """Determine if a failure indicates an unclear specification."""
    failure_lower = failure_reason.lower()

    if any(keyword in failure_lower for keyword in agent.SPEC_UNCLEAR_KEYWORDS):
        return True

    if len(spec.strip()) < agent.MIN_SPEC_LENGTH:
        return True

    return "test failed after" in failure_lower and "different" in failure_lower


def generate_planner_feedback(
    agent: Any,
    issue_number: int,
    spec: str,
    failure_reason: str,
    attempt_count: int,
) -> str:
    """Generate detailed feedback for the Planner."""
    return f"""## Implementation Failure Analysis

**Issue Number:** #{issue_number}
**Attempts Made:** {attempt_count}/{agent.MAX_RETRIES}
**Agent ID:** {agent.agent_id}

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


def comment_worker_escalation(
    agent: Any, issue_number: int, reason: str, details: str
) -> None:
    """Post a normalized worker escalation marker for Planner loop pickup."""
    agent.github.comment_issue(
        issue_number,
        f"ESCALATION:worker\n\nReason: {reason}\n\n{details}",
    )


def _find_uv() -> str:
    """Resolve the uv executable path, falling back to common install locations."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in [
        Path.home() / ".local" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
    ]:
        if candidate.exists():
            return str(candidate)
    return "uv"


def _tool_env(agent: Any) -> dict[str, str]:
    """Build env for uv subprocess calls in a worktree.

    Sets UV_PROJECT_ENVIRONMENT to the main workspace's .venv so that all
    `uv run` commands in worktrees share the fully-installed dev environment
    (pytest, mypy, ruff) without needing a redundant `uv sync` per worktree.
    """
    venv_path = agent.workspace_manager.venv_path
    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(venv_path)
    return env


def run_tests(
    agent: Any, issue_number: int, git_path: Path | None = None
) -> tuple[bool, str]:
    """Run pytest for the specific issue's tests."""
    repo_path = git_path or agent.git.path
    test_path = agent._locate_issue_test_file(issue_number, repo_path)

    if test_path is None:
        return False, f"Test file not found: tests/test_issue_{issue_number}.py"

    test_file = str(test_path.relative_to(repo_path))
    logger.info(f"[{agent.agent_id}] Running tests: {test_file}")

    try:
        result = subprocess.run(
            [_find_uv(), "run", "pytest", test_file, "-v", "--tb=short"],
            cwd=repo_path,
            env=_tool_env(agent),
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        success = result.returncode == 0

        if success:
            logger.info(f"[{agent.agent_id}] Tests passed for issue #{issue_number}")
        else:
            logger.warning(f"[{agent.agent_id}] Tests failed for issue #{issue_number}")

        return success, output
    except subprocess.TimeoutExpired:
        error_msg = "Tests timed out after 5 minutes"
        logger.error(f"[{agent.agent_id}] {error_msg}")
        return False, error_msg
    except Exception as e:
        error_msg = f"Test execution error: {e}"
        logger.error(f"[{agent.agent_id}] {error_msg}")
        return False, error_msg


def auto_format(agent: Any, repo_path: Path) -> None:
    """Run ruff format and ruff check --fix in-place before committing."""
    uv = _find_uv()
    env = _tool_env(agent)
    for cmd in (
        [uv, "run", "ruff", "format", "."],
        [uv, "run", "ruff", "check", "--fix", "."],
    ):
        try:
            subprocess.run(
                cmd, cwd=repo_path, env=env, capture_output=True, text=True, timeout=120
            )
        except Exception:
            pass


def run_quality_checks(agent: Any, git_path: Path | None = None) -> tuple[bool, str]:
    """Run repo-wide quality checks before issue-specific tests."""
    repo_path = git_path or agent.git.path
    uv = _find_uv()
    env = _tool_env(agent)
    checks = [
        ("ruff", [uv, "run", "ruff", "check", "."]),
        ("mypy", [uv, "run", "mypy", "."]),
    ]
    outputs: list[str] = []
    all_passed = True

    for check_name, cmd in checks:
        try:
            result = subprocess.run(
                cmd,
                cwd=repo_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            outputs.append(
                f"{check_name} (exit={result.returncode})\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
            if result.returncode != 0:
                all_passed = False
        except subprocess.TimeoutExpired:
            all_passed = False
            outputs.append(f"{check_name} timed out after 5 minutes")
        except Exception as e:
            all_passed = False
            outputs.append(f"{check_name} execution error: {e}")

    return all_passed, "\n\n".join(outputs)


def snapshot_test_files(agent: Any, repo_path: Path) -> set[Path]:
    """Collect current test files under tests/."""
    tests_dir = repo_path / "tests"
    if not tests_dir.exists():
        return set()
    return {path for path in tests_dir.rglob("test*.py") if path.is_file()}


def ensure_issue_test_file(
    agent: Any, issue_number: int, repo_path: Path, before_files: set[Path]
) -> None:
    """Ensure generated issue test exists at tests/test_issue_<n>.py."""
    expected = repo_path / "tests" / f"test_issue_{issue_number}.py"
    if expected.exists():
        return

    after_files = agent._snapshot_test_files(repo_path)
    new_files = sorted(
        [path for path in after_files if path not in before_files],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if len(new_files) == 1:
        expected.parent.mkdir(parents=True, exist_ok=True)
        source = new_files[0]
        logger.warning(
            f"[{agent.agent_id}] Generated test file {source.relative_to(repo_path)} "
            f"does not match required path; moving to tests/test_issue_{issue_number}.py"
        )
        source.replace(expected)
        return

    logger.warning(
        f"[{agent.agent_id}] Could not normalize generated tests to "
        f"tests/test_issue_{issue_number}.py automatically."
    )


def ensure_retry_tests_available(agent: Any, issue: Issue, feedback: str) -> None:
    """
    Ensure retry flow has an issue-specific test file to execute.

    When tests are missing on the PR branch, regenerate tests before retrying
    implementation so the fix loop remains deterministic.
    """
    if agent._locate_issue_test_file(issue.number, agent.git.path) is not None:
        return

    logger.warning(
        f"[{agent.agent_id}] Missing tests/test_issue_{issue.number}.py on retry branch. "
        "Regenerating tests before implementation retry."
    )
    before_files = agent._snapshot_test_files(agent.git.path)
    test_result = agent.llm.generate_tests(
        spec=(
            f"{issue.body}\n\n"
            "## Review Feedback Context\n"
            f"{feedback}\n\n"
            "## Test File Requirement\n"
            f"Create/overwrite exactly one issue test file at "
            f"`tests/test_issue_{issue.number}.py`."
        ),
        repo_context=(
            f"Repository: {agent.repo}\n"
            f"Required test path: tests/test_issue_{issue.number}.py"
        ),
        work_dir=agent.git.path,
    )
    if not test_result.success:
        raise RuntimeError(
            f"Retry test generation failed: {test_result.error or 'unknown error'}"
        )

    agent._ensure_issue_test_file(issue.number, agent.git.path, before_files)
    if agent._locate_issue_test_file(issue.number, agent.git.path) is None:
        raise RuntimeError(
            f"Retry tests still missing after generation: tests/test_issue_{issue.number}.py"
        )

    agent._auto_format(agent.git.path)
    commit_result = agent.git.commit(
        f"test: refresh tests for #{issue.number} before retry\n\n{issue.title}"
    )
    if not commit_result.success and commit_result.error != "No changes to commit":
        raise RuntimeError(f"Retry test commit failed: {commit_result.error}")


def locate_issue_test_file(
    agent: Any, issue_number: int, repo_path: Path
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


def wait_for_ci(
    agent: Any, pr_number: int, timeout: int | None = None
) -> tuple[bool, str]:
    """Wait for CI to complete."""
    timeout = timeout or agent.CI_WAIT_TIMEOUT
    logger.info(
        f"[{agent.agent_id}] Waiting for CI on PR #{pr_number} (timeout: {timeout}s)"
    )

    elapsed = 0
    none_elapsed = 0
    while elapsed < timeout:
        ci_status = agent.github.get_ci_status(pr_number)

        if ci_status["status"] == "success":
            logger.info(f"[{agent.agent_id}] CI passed on PR #{pr_number}")
            return True, "success"
        if ci_status["status"] == "failure":
            logger.warning(f"[{agent.agent_id}] CI failed on PR #{pr_number}")
            return False, "failure"
        if ci_status["status"] == "none":
            none_elapsed += agent.CI_CHECK_INTERVAL
            if none_elapsed >= agent.CI_NO_CHECKS_GRACE_SECONDS:
                logger.info(
                    f"[{agent.agent_id}] No CI checks detected for "
                    f"{agent.CI_NO_CHECKS_GRACE_SECONDS}s on PR #{pr_number}; "
                    "treating as no CI configured"
                )
                return True, "success"
            logger.debug(
                f"[{agent.agent_id}] CI checks not registered yet on PR #{pr_number} "
                f"(elapsed without checks: {none_elapsed}s)"
            )
        else:
            none_elapsed = 0

        logger.debug(
            f"[{agent.agent_id}] CI pending on PR #{pr_number} "
            f"({ci_status['pending_count']} checks pending, elapsed: {elapsed}s)"
        )
        time.sleep(agent.CI_CHECK_INTERVAL)
        elapsed += agent.CI_CHECK_INTERVAL

    logger.warning(f"[{agent.agent_id}] CI timeout on PR #{pr_number} after {timeout}s")
    return False, "pending"


def get_ci_failure_logs(agent: Any, pr_number: int) -> str:
    """Get formatted CI failure logs for LLM."""
    logger.info(f"[{agent.agent_id}] Fetching CI failure logs for PR #{pr_number}")
    failed_checks = agent.github.get_ci_logs(pr_number)

    if not failed_checks:
        return "CI failed but no detailed logs available."

    logs = ["# CI Failure Report\n"]
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


def extract_issue_number(agent: Any, pr_body: str) -> int | None:
    """Extract issue number from PR body."""
    for pattern in (
        r"[Cc]loses?\s+#(\d+)",
        r"[Ff]ixes?\s+#(\d+)",
        r"[Rr]esolves?\s+#(\d+)",
    ):
        match = re.search(pattern, pr_body)
        if match:
            return int(match.group(1))
    return None


def get_retry_count(agent: Any, issue_number: int, pr_number: int | None = None) -> int:
    """Get current retry count from Issue comments."""
    comments = agent.github.get_issue_comments(issue_number, limit=100)
    retry_count = 0

    for comment in comments:
        body = comment.get("body", "")
        match = re.search(rf"{agent.RETRY_MARKER}:(\d+)", body)
        if not match:
            match = re.search(r"RETRY:(\d+)", body)
        if match:
            retry_count = max(retry_count, int(match.group(1)))

    if pr_number is not None and pr_number != issue_number:
        pr_comments = agent.github.get_issue_comments(pr_number, limit=50)
        for comment in pr_comments:
            match = re.search(r"RETRY:(\d+)", comment.get("body", ""))
            if match:
                retry_count = max(retry_count, int(match.group(1)))

    return retry_count


def record_retry(
    agent: Any,
    issue_number: int,
    pr_number: int,
    retry_count: int,
    error: str | None = None,
) -> None:
    """Record retry attempt in linked Issue comment for persistence."""
    body = (
        f"🔄 **Auto-retry #{retry_count}**\n\n"
        f"{agent.RETRY_MARKER}:{retry_count}\n\n"
        f"Linked PR: #{pr_number}\n\n"
        "Worker Agent is addressing review feedback and regenerating implementation."
    )
    if error:
        body += f"\n\nError: {error}"
    agent.github.comment_issue(issue_number, body)


def get_latest_review_feedback(agent: Any, pr_number: int) -> str:
    """Get the most recent review feedback."""
    reviews = agent.github.get_pr_reviews(pr_number)
    if not reviews:
        return "No review feedback available"

    for review in reversed(reviews):
        if review.get("state") == "CHANGES_REQUESTED":
            body = review.get("body", "").strip()
            if body:
                return str(body)

    for review in reversed(reviews):
        body = review.get("body", "").strip()
        if body:
            return str(body)

    return "No detailed review feedback available"
