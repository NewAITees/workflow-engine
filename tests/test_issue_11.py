"""Issue #11 tests: Worker TDD loop, CI reporting, and reviewer coverage checks."""

from __future__ import annotations

import importlib.util
import re
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.config import AgentConfig
from shared.github_client import Issue
from shared.llm_client import LLMClient, LLMResult

WORKER_SPEC = importlib.util.spec_from_file_location(
    "worker_main",
    Path(__file__).parent.parent / "worker-agent" / "main.py",
)
if WORKER_SPEC is None or WORKER_SPEC.loader is None:
    raise ImportError("Failed to load worker-agent/main.py")
worker_main = importlib.util.module_from_spec(WORKER_SPEC)
sys.modules["worker_main_issue_11"] = worker_main
WORKER_SPEC.loader.exec_module(worker_main)
WorkerAgent = worker_main.WorkerAgent


@pytest.fixture
def worker_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WorkerAgent:
    """Create a WorkerAgent with mocked dependencies for deterministic TDD tests."""
    config = AgentConfig(repo="owner/repo", work_dir=str(tmp_path), llm_backend="codex")
    # New requirement for issue #11: numeric target persisted by Worker.
    setattr(config, "coverage_target", 85)

    github = MagicMock()
    lock = MagicMock()
    llm = MagicMock()
    git = MagicMock()
    git.path = tmp_path / "repo"
    git.path.mkdir(parents=True)
    (git.path / "tests").mkdir(parents=True)
    git.workspace = str(tmp_path / "workspace")

    lock_result = MagicMock()
    lock_result.success = True
    lock.try_lock_issue.return_value = lock_result

    monkeypatch.setattr(worker_main, "get_agent_config", lambda repo, config_path=None: config)
    monkeypatch.setattr(worker_main, "GitHubClient", lambda repo, gh_cli="gh": github)
    monkeypatch.setattr(worker_main, "LockManager", lambda gh, agent_type, agent_id: lock)
    monkeypatch.setattr(worker_main, "LLMClient", lambda cfg: llm)
    monkeypatch.setattr(worker_main, "GitOperations", lambda repo, work_dir: git)
    monkeypatch.setattr(worker_main, "WorkspaceManager", lambda repo, path: MagicMock())

    agent = WorkerAgent("owner/repo")

    github.comment_issue.return_value = True
    github.comment_pr.return_value = True
    github.add_label.return_value = True
    github.remove_label.return_value = True
    github.add_pr_label.return_value = True
    github.remove_pr_label.return_value = True
    github.get_default_branch.return_value = "main"
    github.create_pr.return_value = "https://github.com/owner/repo/pull/42"

    return agent


def test_worker_tdd_flow_sets_numeric_coverage_goal_in_test_generation_prompt(
    worker_agent: WorkerAgent,
) -> None:
    """Worker should generate tests before implementation and include numeric coverage goal."""
    issue = Issue(number=11, title="Issue 11", body="Feature spec", labels=["status:ready"])
    issue_git = MagicMock()
    issue_git.path = worker_agent.git.path
    issue_git.commit.return_value = MagicMock(success=True, error=None)
    issue_git.push.return_value = MagicMock(success=True, error=None)

    calls: list[str] = []

    def generate_tests_side_effect(*args, **kwargs):
        calls.append("tests")
        test_file = issue_git.path / "tests" / "test_issue_11.py"
        test_file.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
        return LLMResult(success=True, output="ok")

    def generate_impl_side_effect(*args, **kwargs):
        calls.append("implementation")
        return LLMResult(success=True, output="ok")

    worker_agent.llm.generate_tests.side_effect = generate_tests_side_effect
    worker_agent.llm.generate_implementation.side_effect = generate_impl_side_effect
    worker_agent._run_quality_checks = MagicMock(return_value=(True, "quality ok"))
    worker_agent._run_tests = MagicMock(return_value=(True, "tests ok"))
    worker_agent._wait_for_ci = MagicMock(return_value=(True, "success"))
    worker_agent._issue_workspace = MagicMock(return_value=nullcontext(issue_git))

    assert worker_agent._try_process_issue(issue) is True
    assert calls[:2] == ["tests", "implementation"]

    spec_prompt = worker_agent.llm.generate_tests.call_args.kwargs["spec"]
    assert "coverage" in spec_prompt.lower()
    assert re.search(r"\\b85\\b", spec_prompt)


@patch("subprocess.run")
def test_run_tests_uses_uv_run_pytest_with_coverage_threshold(
    mock_run: MagicMock,
    worker_agent: WorkerAgent,
) -> None:
    """Worker test execution should use `uv run pytest` and enforce coverage threshold."""
    repo_path = worker_agent.git.path
    test_file = repo_path / "tests" / "test_issue_11.py"
    test_file.write_text("def test_example():\n    assert True\n", encoding="utf-8")

    mock_run.return_value = MagicMock(returncode=0, stdout="passed", stderr="")

    success, _ = worker_agent._run_tests(11, git_path=repo_path)

    assert success is True
    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == ["uv", "run", "pytest"]
    assert any(arg.startswith("--cov-fail-under=") for arg in cmd)


def test_test_fix_loop_retries_three_times_then_escalates_test_design_review(
    worker_agent: WorkerAgent,
) -> None:
    """After 3 failed local test attempts, Worker should escalate test-design review to Planner."""
    issue = Issue(number=11, title="Issue 11", body="Feature spec " * 20, labels=[])
    issue_git = MagicMock()
    issue_git.path = worker_agent.git.path
    issue_git.commit.return_value = MagicMock(success=True, error=None)
    issue_git.push.return_value = MagicMock(success=True, error=None)

    test_file = issue_git.path / "tests" / "test_issue_11.py"
    test_file.write_text("def test_example():\n    assert False\n", encoding="utf-8")

    worker_agent._issue_workspace = MagicMock(return_value=nullcontext(issue_git))
    worker_agent.llm.generate_tests.return_value = LLMResult(success=True, output="ok")
    worker_agent.llm.generate_implementation.return_value = LLMResult(success=True, output="ok")
    worker_agent._run_quality_checks = MagicMock(return_value=(True, "quality ok"))
    worker_agent._run_tests = MagicMock(
        side_effect=[(False, "fail 1"), (False, "fail 2"), (False, "fail 3")]
    )

    assert worker_agent._try_process_issue(issue) is False
    assert worker_agent._run_tests.call_count == 3
    worker_agent.github.add_label.assert_any_call(
        issue.number, worker_agent.STATUS_NEEDS_CLARIFICATION
    )
    assert any(
        "ESCALATION:worker" in str(call)
        and "test design" in str(call).lower()
        for call in worker_agent.github.comment_issue.call_args_list
    )


def test_zero_generated_tests_stops_before_implementation_and_escalates(
    worker_agent: WorkerAgent,
) -> None:
    """If test generation produces zero files, Worker should escalate without implementation attempts."""
    issue = Issue(number=11, title="Issue 11", body="Feature spec " * 20, labels=[])
    issue_git = MagicMock()
    issue_git.path = worker_agent.git.path
    issue_git.commit.return_value = MagicMock(success=True, error=None)

    worker_agent._issue_workspace = MagicMock(return_value=nullcontext(issue_git))
    worker_agent.llm.generate_tests.return_value = LLMResult(success=True, output="ok")
    worker_agent.llm.generate_implementation.return_value = LLMResult(success=True, output="ok")
    worker_agent._run_quality_checks = MagicMock(return_value=(True, "quality ok"))
    worker_agent._run_tests = MagicMock(return_value=(False, "test file not found"))

    assert worker_agent._try_process_issue(issue) is False
    worker_agent.llm.generate_implementation.assert_not_called()
    assert any(
        "ESCALATION:worker" in str(call)
        and "test design" in str(call).lower()
        for call in worker_agent.github.comment_issue.call_args_list
    )


def test_ci_workflow_comments_test_results_and_attaches_coverage_for_prs() -> None:
    """CI workflow should publish PR test results and attach coverage report artifacts."""
    workflow = (Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "pull_request:" in workflow
    assert "--cov" in workflow
    assert "coverage.xml" in workflow or "coverage.json" in workflow
    assert "upload-artifact" in workflow
    assert "pr comment" in workflow.lower() or "comment" in workflow.lower()
    assert "status:changes-requested" in workflow


def test_reviewer_prompt_explicitly_requires_test_code_and_coverage_gap_review() -> None:
    """Reviewer prompt should require test-code review and coverage-gap detection."""
    config = AgentConfig(repo="owner/repo", llm_backend="codex")
    client = LLMClient(config)
    client._run = MagicMock(return_value=LLMResult(success=True, output='{"issues": []}'))

    result = client.review_code_with_severity(
        spec="Implement issue #11",
        diff="diff --git a/tests/test_issue_11.py b/tests/test_issue_11.py",
        repo_context="Repository: owner/repo",
        work_dir=Path("/tmp"),
    )

    assert result.success is True
    prompt = client._run.call_args.args[0]
    assert "Review BOTH production code and test code in the diff" in prompt
    assert "Coverage gaps around critical paths should be reported as at least MAJOR" in prompt
