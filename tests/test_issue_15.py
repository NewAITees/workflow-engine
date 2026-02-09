"""Issue #15: worktree-per-issue TDD tests (pre-implementation)."""

from __future__ import annotations

import importlib.util
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def worker_main_module():
    """Load worker-agent/main.py as a module for direct class access."""
    spec = importlib.util.spec_from_file_location(
        "worker_main_issue_15",
        REPO_ROOT / "worker-agent" / "main.py",
    )
    if spec is None or spec.loader is None:
        raise ImportError("Failed to load worker-agent/main.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules["worker_main_issue_15"] = module
    spec.loader.exec_module(module)
    return module


def _load_worker_git_operations_module():
    """Load worker-agent/git_operations.py (must exist for this feature)."""
    target = REPO_ROOT / "worker-agent" / "git_operations.py"
    assert target.exists(), "worker-agent/git_operations.py must be added"

    spec = importlib.util.spec_from_file_location("worker_git_ops_issue_15", target)
    if spec is None or spec.loader is None:
        raise ImportError("Failed to load worker-agent/git_operations.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules["worker_git_ops_issue_15"] = module
    spec.loader.exec_module(module)
    return module


def _make_agent(worker_main_module, tmp_path: Path):
    """Create a WorkerAgent instance with external dependencies mocked."""
    worker_main_module.get_agent_config = MagicMock(
        return_value=SimpleNamespace(
            work_dir=str(tmp_path / "workspaces"),
            llm_backend="codex",
            gh_cli="gh",
            poll_interval=1,
            stale_lock_timeout_minutes=30,
        )
    )
    worker_main_module.GitHubClient = MagicMock(return_value=MagicMock())
    worker_main_module.LockManager = MagicMock(return_value=MagicMock())
    worker_main_module.LLMClient = MagicMock(return_value=MagicMock())

    git_mock = MagicMock()
    git_mock.workspace = tmp_path / "workspaces" / "owner_repo"
    git_mock.path = git_mock.workspace
    worker_main_module.GitOperations = MagicMock(return_value=git_mock)
    worker_main_module.WorkspaceManager = MagicMock(return_value=MagicMock())

    agent = worker_main_module.WorkerAgent("owner/repo")
    agent.git = git_mock

    return agent


def _configure_happy_path(agent, issue_number: int, issue_title: str, issue_body: str):
    """Configure mocks so _try_process_issue can complete without external calls."""
    from shared.github_client import Issue

    issue = Issue(number=issue_number, title=issue_title, body=issue_body, labels=[])

    agent.lock.try_lock_issue = MagicMock(return_value=SimpleNamespace(success=True))
    agent._snapshot_test_files = MagicMock(return_value=set())
    agent._ensure_issue_test_file = MagicMock()
    agent._run_tests = MagicMock(return_value=(True, "ok"))
    agent._wait_for_ci = MagicMock(return_value=(True, "success"))

    agent.llm.generate_tests = MagicMock(return_value=SimpleNamespace(success=True))
    agent.llm.generate_implementation = MagicMock(
        return_value=SimpleNamespace(success=True)
    )

    issue_git = MagicMock()
    issue_git.path = Path("/tmp/worktree-issue")
    issue_git.commit = MagicMock(return_value=SimpleNamespace(success=True, error=None))
    issue_git.push = MagicMock(return_value=SimpleNamespace(success=True, error=None))

    agent.github.get_default_branch = MagicMock(return_value="main")
    agent.github.create_pr = MagicMock(
        return_value="https://github.com/owner/repo/pull/42"
    )
    agent.github.remove_label = MagicMock(return_value=True)
    agent.github.add_label = MagicMock(return_value=True)
    agent.github.comment_issue = MagicMock(return_value=True)
    agent.github.comment_pr = MagicMock(return_value=True)
    agent.github.add_pr_label = MagicMock(return_value=True)
    agent.github.remove_pr_label = MagicMock(return_value=True)

    return issue, issue_git


def test_worker_git_operations_api_contract_exists():
    """worker-agent/git_operations.py exposes required worktree APIs and GitResult fields."""
    mod = _load_worker_git_operations_module()

    assert hasattr(mod, "GitOperations")
    assert hasattr(mod, "GitResult")

    git_result_fields = {f.name for f in fields(mod.GitResult)}
    assert {"success", "returncode", "stdout", "stderr", "command"}.issubset(
        git_result_fields
    )

    methods = dir(mod.GitOperations)
    assert "create_worktree" in methods
    assert "remove_worktree" in methods
    assert "build_worktree_path" in methods


def test_build_worktree_path_is_unique_and_issue_scoped(tmp_path):
    """Worktree paths should include issue number and remain unique across rapid calls."""
    mod = _load_worker_git_operations_module()
    git = mod.GitOperations("owner/repo", work_base=tmp_path)

    first = git.build_worktree_path(issue_number=15)
    time.sleep(0.005)
    second = git.build_worktree_path(issue_number=15)

    assert first != second
    assert first.parent == second.parent
    assert "issue-15" in first.name
    assert "issue-15" in second.name


def test_create_worktree_runs_git_worktree_add_with_branch_creation(tmp_path):
    """create_worktree must execute `git worktree add -b <branch> <path> <base_branch>`."""
    mod = _load_worker_git_operations_module()
    git = mod.GitOperations("owner/repo", work_base=tmp_path)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch.object(git, "get_default_branch", return_value="main"):
            result = git.create_worktree(issue_number=15, branch_name="auto/issue-15")

    cmd = mock_run.call_args.kwargs["args"] if "args" in mock_run.call_args.kwargs else mock_run.call_args.args[0]

    assert cmd[:5] == ["git", "worktree", "add", "-b", "auto/issue-15"]
    assert "issue-15" in cmd[5]
    assert cmd[-1] == "main"
    assert result.success is True
    assert result.returncode == 0
    assert "worktree" in " ".join(result.command)


def test_remove_worktree_runs_force_variant_when_requested(tmp_path):
    """remove_worktree uses `git worktree remove <path>` and supports `-f` flag."""
    mod = _load_worker_git_operations_module()
    git = mod.GitOperations("owner/repo", work_base=tmp_path)
    target = tmp_path / "owner-repo-issue-15-abc"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        normal = git.remove_worktree(target, force=False)
        forced = git.remove_worktree(target, force=True)

    first_cmd = (
        mock_run.call_args_list[0].kwargs["args"]
        if "args" in mock_run.call_args_list[0].kwargs
        else mock_run.call_args_list[0].args[0]
    )
    second_cmd = (
        mock_run.call_args_list[1].kwargs["args"]
        if "args" in mock_run.call_args_list[1].kwargs
        else mock_run.call_args_list[1].args[0]
    )

    assert first_cmd == ["git", "worktree", "remove", str(target)]
    assert second_cmd == ["git", "worktree", "remove", "-f", str(target)]
    assert normal.success is True
    assert forced.success is True


def test_worker_uses_issue_worktree_path_for_llm_workdir(worker_main_module, tmp_path):
    """Issue execution must pass per-issue worktree path as LLM work_dir."""
    agent = _make_agent(worker_main_module, tmp_path)

    issue_body = "Detailed specification. " * 10
    issue, issue_git = _configure_happy_path(agent, 15, "Issue 15", issue_body)
    issue_git.path = tmp_path / "repo-issue-15-100-200"

    agent._issue_workspace = MagicMock(return_value=nullcontext(issue_git))

    success = agent._try_process_issue(issue)

    assert success is True
    assert (
        agent.llm.generate_tests.call_args.kwargs["work_dir"] == issue_git.path
    )
    assert (
        agent.llm.generate_implementation.call_args.kwargs["work_dir"] == issue_git.path
    )


def test_cleanup_final_failure_marks_failed_and_escalates(worker_main_module, tmp_path):
    """If worktree cleanup ultimately fails, result is failed and escalation marker is emitted."""
    agent = _make_agent(worker_main_module, tmp_path)

    # Keep body long to avoid triggering "spec unclear" fallback escalation path.
    issue_body = "Explicit and concrete acceptance criteria. " * 20
    issue, issue_git = _configure_happy_path(agent, 15, "Issue 15", issue_body)

    @contextmanager
    def broken_workspace(*_args, **_kwargs):
        yield issue_git
        raise RuntimeError("worktree cleanup final failure after retries")

    agent._issue_workspace = broken_workspace
    agent.lock.mark_failed = MagicMock(return_value=True)

    success = agent._try_process_issue(issue)

    assert success is False
    agent.lock.mark_failed.assert_called_once()
    assert any(
        "ESCALATION:worker" in str(call)
        for call in agent.github.comment_issue.call_args_list
    )


def test_readme_mentions_worktree_lifecycle_and_escalation_marker():
    """README should document worktree placement/naming/cleanup and escalation behavior."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "worktree" in readme.lower()
    assert "ESCALATION:worker" in readme
    assert "cleanup" in readme.lower()
