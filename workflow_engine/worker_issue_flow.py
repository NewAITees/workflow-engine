"""Issue implementation flow for WorkerAgent."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from shared.git_operations import GitOperations
from shared.github_client import Issue

logger = logging.getLogger("worker-agent")


def process_ready_issues(agent: Any) -> None:
    """Find and process ready issues."""
    issues = agent.github.list_issues(labels=[agent.STATUS_READY])

    if not issues:
        logger.debug("No ready issues found")
        return

    logger.info(f"Found {len(issues)} ready issue(s)")
    for issue in issues:
        if agent._try_process_issue(issue):
            logger.info(f"Successfully processed issue #{issue.number}")
        else:
            logger.debug(f"Skipped issue #{issue.number}")


@contextmanager
def issue_workspace(
    agent: Any, issue_number: int, branch_name: str
) -> Generator[GitOperations, None, None]:
    """Provide a per-issue worktree and fall back to legacy workspace if needed."""
    if not isinstance(agent.git, GitOperations):
        clone_result = agent.git.clone_or_pull()
        if not clone_result.success:
            raise RuntimeError(f"Failed to prepare workspace: {clone_result.error}")

        branch_result = agent.git.create_branch(branch_name)
        if not branch_result.success:
            raise RuntimeError(f"Failed to create branch: {branch_result.error}")

        yield agent.git
        return

    default_branch = agent.github.get_default_branch()
    worktree_id = f"{agent.agent_id}-issue-{issue_number}"
    worktree_cm = None
    work_git = None

    try:
        worktree_cm = agent.workspace_manager.worktree(
            branch_name,
            worktree_id,
            create_branch=True,
            base_branch=default_branch,
        )
        work_git = worktree_cm.__enter__()
    except Exception as e:
        if "already exists" in str(e):
            logger.warning(
                f"[{agent.agent_id}] Worktree branch already exists for issue #{issue_number}; "
                "retrying by reusing existing branch."
            )
            try:
                worktree_cm = agent.workspace_manager.worktree(
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
                f"[{agent.agent_id}] Worktree setup failed for issue #{issue_number}: {e}. "
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

    clone_result = agent.git.clone_or_pull()
    if not clone_result.success:
        raise RuntimeError(f"Failed to prepare workspace: {clone_result.error}")

    branch_result = agent.git.create_branch(branch_name)
    if not branch_result.success:
        raise RuntimeError(f"Failed to create branch: {branch_result.error}")

    yield agent.git


def try_process_issue(agent: Any, issue: Issue) -> bool:
    """Try to process a single issue with the TDD flow."""
    logger.info(
        f"[{agent.agent_id}] Attempting to process issue #{issue.number}: {issue.title}"
    )
    test_retry_count = 0

    lock_result = agent.lock.try_lock_issue(
        issue.number,
        agent.STATUS_READY,
        agent.STATUS_IMPLEMENTING,
    )
    if not lock_result.success:
        logger.debug(f"Could not lock issue #{issue.number}: {lock_result.error}")
        return False

    branch_name = f"auto/issue-{issue.number}"

    try:
        with agent._issue_workspace(issue.number, branch_name) as issue_git:
            before_test_files = agent._snapshot_test_files(issue_git.path)

            logger.info(
                f"[{agent.agent_id}] Generating tests with {agent.config.llm_backend}..."
            )
            test_result = agent.llm.generate_tests(
                spec=(
                    f"{issue.body}\n\n"
                    "## Test File Requirement\n"
                    f"Create/overwrite exactly one issue test file at "
                    f"`tests/test_issue_{issue.number}.py`."
                    f"{agent.CODE_QUALITY_REQUIREMENTS}"
                ),
                repo_context=(
                    f"Repository: {agent.repo}\n"
                    f"Required test path: tests/test_issue_{issue.number}.py"
                ),
                work_dir=issue_git.path,
            )
            if not test_result.success:
                raise RuntimeError(f"Test generation failed: {test_result.error}")

            agent._ensure_issue_test_file(
                issue.number, issue_git.path, before_test_files
            )

            logger.info(f"[{agent.agent_id}] Committing tests...")
            agent._auto_format(issue_git.path)
            test_commit_result = issue_git.commit(
                f"test: add tests for #{issue.number}\n\n{issue.title}"
            )
            if not test_commit_result.success:
                if test_commit_result.error == "No changes to commit":
                    logger.info(
                        f"[{agent.agent_id}] No test file changes detected (no-op), skipping test commit."
                    )
                else:
                    raise RuntimeError(
                        f"Test commit failed: {test_commit_result.error}"
                    )

            validation_failure_output = ""
            test_passed = False

            while test_retry_count < agent.MAX_RETRIES:
                logger.info(
                    f"[{agent.agent_id}] Generating implementation "
                    f"(attempt {test_retry_count + 1}/{agent.MAX_RETRIES})..."
                )
                impl_spec = issue.body + agent.CODE_QUALITY_REQUIREMENTS
                if test_retry_count > 0:
                    impl_spec += (
                        f"\n\n## Previous Validation Failure (Attempt {test_retry_count})\n"
                        "Please fix the implementation to satisfy quality checks and tests.\n\n"
                        f"```\n{validation_failure_output[:2000]}\n```"
                    )

                gen_result = agent.llm.generate_implementation(
                    spec=impl_spec,
                    repo_context=(
                        f"Repository: {agent.repo}\n"
                        f"TDD attempt: {test_retry_count + 1}/{agent.MAX_RETRIES}"
                    ),
                    work_dir=issue_git.path,
                )
                if not gen_result.success:
                    raise RuntimeError(f"Implementation failed: {gen_result.error}")

                logger.info(f"[{agent.agent_id}] Committing implementation...")
                agent._auto_format(issue_git.path)
                impl_commit_result = issue_git.commit(
                    f"feat: implement #{issue.number} (attempt {test_retry_count + 1})\n\n"
                    f"{issue.title}"
                )
                if not impl_commit_result.success:
                    if impl_commit_result.error == "No changes to commit":
                        logger.info(
                            f"[{agent.agent_id}] Implementation produced no changes (no-op), skipping commit."
                        )
                    else:
                        raise RuntimeError(
                            f"Implementation commit failed: {impl_commit_result.error}"
                        )

                logger.info(f"[{agent.agent_id}] Running quality checks (ruff/mypy)...")
                quality_passed, quality_output = agent._run_quality_checks(
                    git_path=issue_git.path
                )
                if not quality_passed:
                    test_retry_count += 1
                    validation_failure_output = quality_output
                    logger.warning(
                        f"[{agent.agent_id}] Quality checks failed (attempt {test_retry_count}/{agent.MAX_RETRIES})"
                    )
                    agent.github.comment_issue(
                        issue.number,
                        f"⚠️ **Quality checks failed (attempt {test_retry_count}/{agent.MAX_RETRIES})**\n\n"
                        "Retrying implementation with quality feedback...\n\n"
                        "<details>\n<summary>Quality output</summary>\n\n"
                        f"```\n{quality_output[:2000]}\n```\n\n</details>",
                    )
                    if test_retry_count >= agent.MAX_RETRIES:
                        raise RuntimeError(
                            f"Quality checks failed after {agent.MAX_RETRIES} attempts:\n"
                            f"{quality_output[:500]}"
                        )
                    continue

                agent.github.remove_label(issue.number, agent.STATUS_IMPLEMENTING)
                agent.github.add_label(issue.number, agent.STATUS_TESTING)

                logger.info(f"[{agent.agent_id}] Running tests...")
                test_passed, test_output = agent._run_tests(
                    issue.number, git_path=issue_git.path
                )
                if test_passed:
                    logger.info(
                        f"[{agent.agent_id}] Tests passed on attempt {test_retry_count + 1}"
                    )
                    break

                test_retry_count += 1
                validation_failure_output = test_output
                logger.warning(
                    f"[{agent.agent_id}] Tests failed (attempt {test_retry_count}/{agent.MAX_RETRIES})"
                )
                agent.github.comment_issue(
                    issue.number,
                    f"⚠️ **Test failed (attempt {test_retry_count}/{agent.MAX_RETRIES})**\n\n"
                    "Retrying implementation with test feedback...\n\n"
                    "<details>\n<summary>Test output</summary>\n\n"
                    f"```\n{test_output[:2000]}\n```\n\n</details>",
                )
                if test_retry_count >= agent.MAX_RETRIES:
                    raise RuntimeError(
                        f"Tests failed after {agent.MAX_RETRIES} attempts:\n{test_output[:500]}"
                    )

                agent.github.remove_label(issue.number, agent.STATUS_TESTING)
                agent.github.add_label(issue.number, agent.STATUS_IMPLEMENTING)

            agent.github.remove_label(issue.number, agent.STATUS_TESTING)

            logger.info(f"[{agent.agent_id}] Pushing to remote...")
            push_result = issue_git.push(branch_name, force=True)
            if not push_result.success:
                raise RuntimeError(f"Push failed: {push_result.error}")

            logger.info(f"[{agent.agent_id}] Creating pull request...")
            pr_body = f"""## Summary
Auto-generated implementation for #{issue.number} using TDD approach.

## Original Issue
{issue.title}

## Implementation
Generated by Worker Agent ({agent.agent_id}) using {agent.config.llm_backend}.

**TDD Process:**
- ✅ Tests generated first
- ✅ Implementation created
- ✅ All tests passed (attempts: {test_retry_count + 1}/{agent.MAX_RETRIES})

Closes #{issue.number}

---
🤖 Auto-generated by Workflow Engine Worker Agent
"""
            pr_url = agent.github.create_pr(
                title=f"Auto: {issue.title}",
                body=pr_body,
                head=branch_name,
                base=agent.github.get_default_branch(),
                labels=[agent.STATUS_REVIEWING],
            )
            if not pr_url:
                raise RuntimeError("Failed to create pull request")

            logger.info(f"[{agent.agent_id}] Pull request created: {pr_url}")
            pr_number = int(pr_url.split("/")[-1])
            agent.github.add_label(issue.number, agent.STATUS_REVIEWING)
            agent.github.comment_pr(
                pr_number,
                f"🧪 **Local TDD validation passed**\n\n"
                f"- Tests generated before implementation\n"
                f"- Local test attempts: {test_retry_count + 1}/{agent.MAX_RETRIES}\n"
                f"- Waiting for CI verification",
            )

            ci_retry_count = 0
            logger.info(f"[{agent.agent_id}] Waiting for CI to complete...")
            ci_passed, ci_status = agent._wait_for_ci(
                pr_number, timeout=agent.CI_WAIT_TIMEOUT
            )
            while (
                not ci_passed
                and ci_status == "failure"
                and ci_retry_count < agent.MAX_CI_RETRIES
            ):
                ci_retry_count += 1
                logger.warning(
                    f"[{agent.agent_id}] CI failed (attempt {ci_retry_count}/{agent.MAX_CI_RETRIES})"
                )
                ci_logs = agent._get_ci_failure_logs(pr_number)
                agent.github.comment_pr(
                    pr_number,
                    f"⚠️ **CI failed (attempt {ci_retry_count}/{agent.MAX_CI_RETRIES})**\n\n"
                    "Analyzing failures and attempting automatic fix...\n\n"
                    "<details>\n<summary>CI failure details</summary>\n\n"
                    f"{ci_logs[:2000]}\n\n</details>",
                )
                fix_result = agent.llm.generate_implementation(
                    spec=(
                        f"{issue.body}\n\n"
                        f"## CI Failure (Attempt {ci_retry_count})\n"
                        "The implementation passed local tests but CI checks failed.\n"
                        "Please analyze and fix the CI failures.\n\n"
                        f"{ci_logs}\n"
                    ),
                    repo_context=(
                        f"Repository: {agent.repo}\n"
                        f"CI fix attempt: {ci_retry_count}/{agent.MAX_CI_RETRIES}"
                    ),
                    work_dir=issue_git.path,
                )
                if not fix_result.success:
                    logger.error(
                        f"[{agent.agent_id}] CI fix generation failed: {fix_result.error}"
                    )
                    break

                agent._auto_format(issue_git.path)
                fix_commit_result = issue_git.commit(
                    f"fix: address CI failures for #{issue.number} (attempt {ci_retry_count})\n\n"
                    f"{ci_logs[:200]}"
                )
                if not fix_commit_result.success:
                    if fix_commit_result.error == "No changes to commit":
                        logger.info(
                            f"[{agent.agent_id}] CI fix produced no changes (no-op), skipping commit."
                        )
                    else:
                        logger.error(
                            f"[{agent.agent_id}] CI fix commit failed: {fix_commit_result.error}"
                        )
                        break

                push_result = issue_git.push(branch_name)
                if not push_result.success:
                    logger.error(
                        f"[{agent.agent_id}] CI fix push failed: {push_result.error}"
                    )
                    break

                logger.info(
                    f"[{agent.agent_id}] Waiting {agent.CI_POST_PUSH_SETTLE_SECONDS}s "
                    "for new CI checks to register..."
                )
                time.sleep(agent.CI_POST_PUSH_SETTLE_SECONDS)
                logger.info(f"[{agent.agent_id}] Waiting for CI after fix...")
                ci_passed, ci_status = agent._wait_for_ci(
                    pr_number, timeout=agent.CI_WAIT_TIMEOUT
                )

            if not ci_passed:
                if ci_status == "pending":
                    logger.warning(
                        f"[{agent.agent_id}] CI still pending after timeout, marking for manual review"
                    )
                    agent.github.comment_pr(
                        pr_number,
                        "⏱️ **CI checks are taking longer than expected**\n\n"
                        "The PR is ready for manual review while CI completes.",
                    )
                elif ci_status == "failure":
                    logger.error(
                        f"[{agent.agent_id}] CI failed after {ci_retry_count} fix attempts"
                    )
                    agent.github.add_pr_label(pr_number, agent.STATUS_CI_FAILED)
                    agent.github.add_pr_label(pr_number, agent.STATUS_CHANGES_REQUESTED)
                    agent.github.remove_pr_label(pr_number, agent.STATUS_REVIEWING)
                    agent.github.comment_pr(
                        pr_number,
                        f"❌ **CI checks failed after {ci_retry_count} automatic fix attempts**\n\n"
                        "Manual intervention required. Please review the CI logs and fix the issues.\n\n"
                        f"The PR has been marked with `{agent.STATUS_CI_FAILED}` label.",
                    )
                    agent.github.comment_issue(
                        issue.number,
                        f"⚠️ **CI checks failed**\n\n"
                        f"Pull Request: {pr_url}\n\n"
                        f"Attempted {ci_retry_count} automatic fixes but CI still failing.\n"
                        "Manual review needed.",
                    )
                    return False
            else:
                logger.info(f"[{agent.agent_id}] CI passed!")
                if ci_retry_count > 0:
                    agent.github.comment_pr(
                        pr_number,
                        f"✅ **CI now passing after {ci_retry_count} automatic fix(es)!**",
                    )

            ci_info = ""
            if ci_retry_count > 0:
                ci_info = f"\n- CI fixes: {ci_retry_count} automatic fix(es) applied"

            agent.github.comment_issue(
                issue.number,
                f"✅ **Implementation complete with TDD!**\n\n"
                "- Tests generated and passed\n"
                f"- Test attempts: {test_retry_count + 1}/{agent.MAX_RETRIES}{ci_info}\n\n"
                f"Pull Request: {pr_url}",
            )
            return True

    except Exception as e:
        logger.error(f"[{agent.agent_id}] Failed to process issue #{issue.number}: {e}")
        failure_reason = str(e)
        if "Tests failed after" in failure_reason:
            agent._comment_worker_escalation(
                issue.number,
                "test-retry-limit",
                "Test fix loop exhausted maximum retries. Please review test design and specification.",
            )
            agent.github.remove_label(issue.number, agent.STATUS_IMPLEMENTING)
            agent.github.add_label(issue.number, agent.STATUS_NEEDS_CLARIFICATION)
        elif agent._is_specification_unclear(failure_reason, issue.body):
            agent.lock.mark_needs_clarification(
                issue.number,
                agent.STATUS_IMPLEMENTING,
                failure_reason,
            )
            feedback = agent._generate_planner_feedback(
                issue_number=issue.number,
                spec=issue.body,
                failure_reason=failure_reason,
                attempt_count=test_retry_count + 1,
            )
            agent.github.comment_issue(
                issue.number,
                f"⚠️ **Implementation failed - Specification clarification needed**\n\n"
                f"{feedback}\n\n"
                "@Planner: Please review and clarify the specification.",
            )
            agent._comment_worker_escalation(
                issue.number,
                "spec-clarification-needed",
                "Specification is ambiguous or incomplete for implementation.",
            )
            agent.github.remove_label(issue.number, agent.STATUS_IMPLEMENTING)
            agent.github.add_label(issue.number, agent.STATUS_NEEDS_CLARIFICATION)
        else:
            agent.lock.mark_failed(
                issue.number,
                agent.STATUS_IMPLEMENTING,
                failure_reason,
            )

        agent.git.cleanup_branch(f"auto/issue-{issue.number}")
        return False
