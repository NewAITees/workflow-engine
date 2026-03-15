"""PR retry flow for WorkerAgent."""

from __future__ import annotations

import logging
from typing import Any

from shared.github_client import PullRequest

logger = logging.getLogger("worker-agent")


def process_changes_requested_prs(agent: Any) -> None:
    """Find and retry PRs with changes requested."""
    prs = agent.github.list_prs(labels=[agent.STATUS_CHANGES_REQUESTED])

    if not prs:
        logger.debug("No PRs with changes requested")
        return

    logger.info(f"Found {len(prs)} PR(s) with changes requested")
    for pr in prs:
        if agent._try_retry_pr(pr):
            logger.info(f"Successfully retried PR #{pr.number}")


def try_retry_pr(agent: Any, pr: PullRequest) -> bool:
    """Try to retry a PR with changes requested using the TDD flow."""
    logger.info(f"[{agent.agent_id}] Attempting to retry PR #{pr.number}: {pr.title}")

    issue_number = agent._extract_issue_number(pr.body)
    if not issue_number:
        logger.error(
            f"[{agent.agent_id}] PR #{pr.number} has no linked issue; cannot retry safely"
        )
        agent.github.comment_pr(
            pr.number,
            "⚠️ Cannot determine linked issue from PR body. "
            "Auto-retry has been stopped for safety.",
        )
        agent.github.remove_pr_label(pr.number, agent.STATUS_CHANGES_REQUESTED)
        agent.github.add_pr_label(pr.number, agent.STATUS_FAILED)
        return False

    retry_count = agent._get_retry_count(issue_number, pr.number)
    logger.info(
        f"[{agent.agent_id}] Issue #{issue_number} retry count: {retry_count}/{agent.MAX_RETRIES}"
    )
    if retry_count >= agent.MAX_RETRIES:
        logger.warning(
            f"[{agent.agent_id}] PR #{pr.number} exceeded max retries, escalating to Planner"
        )
        agent.github.comment_issue(
            issue_number,
            f"⚠️ **Auto-retry failed after {agent.MAX_RETRIES} attempts**\n\n"
            f"PR #{pr.number} could not pass review.\n\n"
            "**Action needed**: Please review the specification and provide more details or clarification.\n\n"
            f"Review feedback:\n{agent._get_latest_review_feedback(pr.number)}",
        )
        agent._comment_worker_escalation(
            issue_number,
            "review-retry-limit",
            f"PR #{pr.number} exceeded retry limit ({agent.MAX_RETRIES}).",
        )
        agent.github.remove_pr_label(pr.number, agent.STATUS_CHANGES_REQUESTED)
        agent.github.add_pr_label(pr.number, agent.STATUS_FAILED)
        return False

    lock_result = agent.lock.try_lock_pr(
        pr.number,
        agent.STATUS_CHANGES_REQUESTED,
        agent.STATUS_IMPLEMENTING,
    )
    if not lock_result.success:
        logger.debug(f"Could not lock PR #{pr.number}: {lock_result.error}")
        return False

    try:
        issue = agent.github.get_issue(issue_number)
        if not issue:
            raise RuntimeError(f"Issue #{issue_number} not found")

        feedback = agent._get_latest_review_feedback(pr.number)

        logger.info(f"[{agent.agent_id}] Preparing workspace...")
        clone_result = agent.git.clone_or_pull()
        if not clone_result.success:
            raise RuntimeError(f"Failed to prepare workspace: {clone_result.error}")

        branch_name = pr.head_ref
        logger.info(f"[{agent.agent_id}] Updating branch: {branch_name}")
        branch_result = agent.git.checkout_branch_from_remote(branch_name)
        if not branch_result.success:
            raise RuntimeError(f"Failed to update branch: {branch_result.error}")

        agent._ensure_retry_tests_available(issue, feedback)

        test_retry_count = 0
        validation_failure_output = ""
        test_passed = False

        while test_retry_count < agent.MAX_RETRIES:
            logger.info(
                f"[{agent.agent_id}] Regenerating implementation with feedback "
                f"(test attempt {test_retry_count + 1}/{agent.MAX_RETRIES})..."
            )
            impl_spec = (
                f"{issue.body}{agent.CODE_QUALITY_REQUIREMENTS}\n\n"
                "## Review Feedback (Please address these issues)\n"
                f"{feedback}"
            )
            if test_retry_count > 0:
                impl_spec += (
                    f"\n\n## Validation Failure (Attempt {test_retry_count})\n"
                    "Please fix the implementation to satisfy quality checks and tests.\n\n"
                    f"```\n{validation_failure_output[:2000]}\n```"
                )

            gen_result = agent.llm.generate_implementation(
                spec=impl_spec,
                repo_context=(
                    f"Repository: {agent.repo}\n"
                    f"Review retry: {retry_count + 1}/{agent.MAX_RETRIES}\n"
                    f"Test attempt: {test_retry_count + 1}/{agent.MAX_RETRIES}"
                ),
                work_dir=agent.git.path,
            )
            if not gen_result.success:
                raise RuntimeError(f"Implementation failed: {gen_result.error}")

            logger.info(f"[{agent.agent_id}] Committing changes...")
            agent._auto_format(agent.git.path)
            commit_result = agent.git.commit(
                f"fix: address review feedback for #{issue.number} "
                f"(retry {retry_count + 1}, test attempt {test_retry_count + 1})\n\n"
                f"{feedback[:500]}"
            )
            if not commit_result.success:
                if commit_result.error == "No changes to commit":
                    logger.info(
                        f"[{agent.agent_id}] Implementation produced no changes (no-op), skipping commit."
                    )
                else:
                    raise RuntimeError(f"Commit failed: {commit_result.error}")

            logger.info(f"[{agent.agent_id}] Running quality checks (ruff/mypy)...")
            quality_passed, quality_output = agent._run_quality_checks(
                git_path=agent.git.path
            )
            if not quality_passed:
                test_retry_count += 1
                validation_failure_output = quality_output
                logger.warning(
                    f"[{agent.agent_id}] Quality checks failed (attempt {test_retry_count}/{agent.MAX_RETRIES})"
                )
                agent.github.comment_pr(
                    pr.number,
                    f"⚠️ **Quality checks failed during retry (attempt {test_retry_count}/{agent.MAX_RETRIES})**\n\n"
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

            agent.github.remove_pr_label(pr.number, agent.STATUS_IMPLEMENTING)
            agent.github.add_pr_label(pr.number, agent.STATUS_TESTING)

            logger.info(f"[{agent.agent_id}] Running tests...")
            test_passed, test_output = agent._run_tests(
                issue_number, git_path=agent.git.path
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
            agent.github.comment_pr(
                pr.number,
                f"⚠️ **Test failed during retry (attempt {test_retry_count}/{agent.MAX_RETRIES})**\n\n"
                "Retrying implementation with test feedback...\n\n"
                "<details>\n<summary>Test output</summary>\n\n"
                f"```\n{test_output[:2000]}\n```\n\n</details>",
            )
            if test_retry_count >= agent.MAX_RETRIES:
                raise RuntimeError(
                    f"Tests failed after {agent.MAX_RETRIES} attempts:\n{test_output[:500]}"
                )

            agent.github.remove_pr_label(pr.number, agent.STATUS_TESTING)
            agent.github.add_pr_label(pr.number, agent.STATUS_IMPLEMENTING)

        agent.github.remove_pr_label(pr.number, agent.STATUS_TESTING)

        logger.info(f"[{agent.agent_id}] Force pushing to update PR...")
        push_result = agent.git.push(branch_name, force=True)
        if not push_result.success:
            raise RuntimeError(f"Push failed: {push_result.error}")

        agent._record_retry(issue_number, pr.number, retry_count + 1)
        agent.github.add_pr_label(pr.number, agent.STATUS_REVIEWING)

        logger.info(
            f"[{agent.agent_id}] PR #{pr.number} updated with TDD, ready for re-review"
        )

        ci_retry_count = 0
        logger.info(f"[{agent.agent_id}] Waiting for CI to complete...")
        ci_passed, ci_status = agent._wait_for_ci(
            pr.number, timeout=agent.CI_WAIT_TIMEOUT
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
            ci_logs = agent._get_ci_failure_logs(pr.number)
            agent.github.comment_pr(
                pr.number,
                f"⚠️ **CI failed (attempt {ci_retry_count}/{agent.MAX_CI_RETRIES})**\n\n"
                "Analyzing failures and attempting automatic fix...\n\n"
                "<details>\n<summary>CI failure details</summary>\n\n"
                f"{ci_logs[:2000]}\n\n</details>",
            )
            fix_result = agent.llm.generate_implementation(
                spec=(
                    f"{issue.body}\n\n"
                    "## Review Feedback\n"
                    f"{feedback}\n\n"
                    f"## CI Failure (Attempt {ci_retry_count})\n"
                    "The implementation passed local tests but CI checks failed.\n"
                    "Please analyze and fix the CI failures.\n\n"
                    f"{ci_logs}\n"
                ),
                repo_context=(
                    f"Repository: {agent.repo}\n"
                    f"CI fix attempt: {ci_retry_count}/{agent.MAX_CI_RETRIES}"
                ),
                work_dir=agent.git.path,
            )
            if not fix_result.success:
                logger.error(
                    f"[{agent.agent_id}] CI fix generation failed: {fix_result.error}"
                )
                break

            agent._auto_format(agent.git.path)
            fix_commit_result = agent.git.commit(
                f"fix: address CI failures for PR #{pr.number} (attempt {ci_retry_count})\n\n"
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

            push_result = agent.git.push(branch_name, force=True)
            if not push_result.success:
                logger.error(
                    f"[{agent.agent_id}] CI fix push failed: {push_result.error}"
                )
                break

            logger.info(f"[{agent.agent_id}] Waiting for CI after fix...")
            ci_passed, ci_status = agent._wait_for_ci(
                pr.number, timeout=agent.CI_WAIT_TIMEOUT
            )

        if not ci_passed:
            if ci_status == "pending":
                logger.warning(
                    f"[{agent.agent_id}] CI still pending after timeout, marking for manual review"
                )
                agent.github.comment_pr(
                    pr.number,
                    "⏱️ **CI checks are taking longer than expected**\n\n"
                    "The PR is ready for manual review while CI completes.",
                )
            elif ci_status == "failure":
                logger.error(
                    f"[{agent.agent_id}] CI failed after {ci_retry_count} fix attempts"
                )
                agent.github.add_pr_label(pr.number, agent.STATUS_CI_FAILED)
                agent.github.add_pr_label(pr.number, agent.STATUS_CHANGES_REQUESTED)
                agent.github.remove_pr_label(pr.number, agent.STATUS_REVIEWING)
                agent.github.comment_pr(
                    pr.number,
                    f"❌ **CI checks failed after {ci_retry_count} automatic fix attempts**\n\n"
                    "Manual intervention required. Please review the CI logs and fix the issues.\n\n"
                    f"The PR has been marked with `{agent.STATUS_CI_FAILED}` label.",
                )
                return False
        else:
            logger.info(f"[{agent.agent_id}] CI passed!")
            if ci_retry_count > 0:
                agent.github.comment_pr(
                    pr.number,
                    f"✅ **CI now passing after {ci_retry_count} automatic fix(es)!**",
                )

        ci_info = ""
        if ci_retry_count > 0:
            ci_info = f"\n- CI fixes: {ci_retry_count} automatic fix(es) applied"

        agent.github.comment_pr(
            pr.number,
            f"✅ **Implementation updated with TDD!**\n\n"
            "- Addressed review feedback\n"
            f"- All tests passed (attempts: {test_retry_count + 1}/{agent.MAX_RETRIES}){ci_info}\n"
            "- Ready for re-review\n\n"
            f"Review retry: {retry_count + 1}/{agent.MAX_RETRIES}",
        )
        return True

    except Exception as e:
        logger.error(f"[{agent.agent_id}] Failed to retry PR #{pr.number}: {e}")
        failed_retry_count = retry_count + 1
        agent._record_retry(issue_number, pr.number, failed_retry_count, error=str(e))

        if failed_retry_count >= agent.MAX_RETRIES:
            agent.github.comment_issue(
                issue_number,
                f"⚠️ **Auto-retry failed after {agent.MAX_RETRIES} attempts**\n\n"
                f"PR #{pr.number} could not be stabilized due to repeated retry errors.\n\n"
                "Please review the specification and/or implementation strategy.",
            )
            agent._comment_worker_escalation(
                issue_number,
                "review-retry-error-limit",
                f"PR #{pr.number} retry flow failed repeatedly and exhausted retry budget.",
            )
            agent.github.remove_pr_label(pr.number, agent.STATUS_CHANGES_REQUESTED)
            agent.github.remove_pr_label(pr.number, agent.STATUS_IMPLEMENTING)
            agent.github.remove_pr_label(pr.number, agent.STATUS_TESTING)
            agent.github.add_pr_label(pr.number, agent.STATUS_FAILED)
            return False

        agent.github.remove_pr_label(pr.number, agent.STATUS_IMPLEMENTING)
        agent.github.remove_pr_label(pr.number, agent.STATUS_TESTING)
        agent.github.add_pr_label(pr.number, agent.STATUS_CHANGES_REQUESTED)
        agent.github.comment_pr(
            pr.number,
            f"⚠️ Auto-retry failed: {e}\n\nPlease review manually or update the specification.",
        )
        return False
