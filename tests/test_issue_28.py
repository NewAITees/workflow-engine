"""TDD tests for Issue #28 dry-run support across all agents."""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.config import AgentConfig
from shared.git_operations import GitOperations, GitResult
from shared.github_client import GitHubClient

ROOT = Path(__file__).parent.parent


def _load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


planner_main = _load_module("planner_main_issue_28", "planner-agent/main.py")
worker_main = _load_module("worker_main_issue_28", "worker-agent/main.py")
reviewer_main = _load_module("reviewer_main_issue_28", "reviewer-agent/main.py")

WorkerAgent = worker_main.WorkerAgent


@pytest.mark.parametrize("mode", ["execute-tests", "simulate-all"])
def test_agent_config_accepts_dry_run_modes(mode: str) -> None:
    """AgentConfig should accept the two supported dry-run modes."""
    config = AgentConfig(repo="owner/repo", dry_run=mode)

    assert config.dry_run == mode


def test_agent_config_rejects_invalid_dry_run_mode() -> None:
    """AgentConfig should reject unsupported dry-run mode values."""
    with pytest.raises(ValueError, match="dry_run"):
        AgentConfig(repo="owner/repo", dry_run="invalid-mode")


@patch("subprocess.run")
def test_github_write_operation_is_suppressed_and_logged_in_dry_run(
    mock_run: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """GitHub write operations should be blocked and logged as would-run."""
    caplog.set_level(logging.INFO)
    client = GitHubClient("owner/repo", dry_run="execute-tests")

    result = client.add_label(123, "status:ready")

    assert result is True
    mock_run.assert_not_called()
    assert "[DRY-RUN]" in caplog.text
    assert "would run" in caplog.text
    assert "issue edit 123" in caplog.text


@patch("subprocess.run")
def test_github_read_operation_still_executes_in_dry_run(mock_run: MagicMock) -> None:
    """Read-only GitHub operations should still execute in dry-run mode."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue",
                    "body": "Body",
                    "labels": [{"name": "status:ready"}],
                    "state": "open",
                }
            ]
        ),
        stderr="",
    )
    client = GitHubClient("owner/repo", dry_run="simulate-all")

    issues = client.list_issues(labels=["status:ready"])

    assert len(issues) == 1
    mock_run.assert_called_once()


@patch.object(GitOperations, "_run")
def test_git_write_operation_is_suppressed_and_logged_in_dry_run(
    mock_run: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Git write operations should be blocked and logged as would-run."""
    caplog.set_level(logging.INFO)
    git = GitOperations("owner/repo", work_base=tmp_path, dry_run="execute-tests")

    result = git.commit("feat: dry-run guard")

    assert result.success is True
    mock_run.assert_not_called()
    assert "[DRY-RUN]" in caplog.text
    assert "would run" in caplog.text
    assert "git commit" in caplog.text


@patch.object(GitOperations, "_run")
def test_git_read_operation_still_executes_in_dry_run(
    mock_run: MagicMock, tmp_path: Path
) -> None:
    """Read-only git operations should still execute in dry-run mode."""
    mock_run.return_value = GitResult(success=True, output="On branch main\n")
    git = GitOperations("owner/repo", work_base=tmp_path, dry_run="simulate-all")

    status = git.get_status()

    assert "On branch" in status
    mock_run.assert_called_once_with(["status"])


def test_worker_run_tests_is_simulated_in_simulate_all_mode(
    tmp_path: Path,
) -> None:
    """simulate-all should skip local pytest execution and return simulated success."""
    agent = WorkerAgent.__new__(WorkerAgent)
    agent.agent_id = "worker-test"
    agent.config = MagicMock(dry_run="simulate-all")
    agent.git = MagicMock()
    agent.git.path = tmp_path

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_issue_28.py").write_text("def test_x():\n    assert True\n")

    with patch("subprocess.run") as mock_run:
        success, output = agent._run_tests(28)

    assert success is True
    assert "[DRY-RUN]" in output
    mock_run.assert_not_called()


def test_worker_run_tests_executes_in_execute_tests_mode(tmp_path: Path) -> None:
    """execute-tests should run local pytest even when GitHub/Git writes are blocked."""
    agent = WorkerAgent.__new__(WorkerAgent)
    agent.agent_id = "worker-test"
    agent.config = MagicMock(dry_run="execute-tests")
    agent.git = MagicMock()
    agent.git.path = tmp_path

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_issue_28.py").write_text("def test_x():\n    assert True\n")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        success, _ = agent._run_tests(28)

    assert success is True
    mock_run.assert_called_once()


def test_worker_wait_for_ci_is_simulated_in_simulate_all_mode() -> None:
    """simulate-all should skip CI polling and return a simulated success status."""
    agent = WorkerAgent.__new__(WorkerAgent)
    agent.agent_id = "worker-test"
    agent.config = MagicMock(dry_run="simulate-all")
    agent.github = MagicMock()
    agent.CI_CHECK_INTERVAL = 1
    agent.CI_WAIT_TIMEOUT = 10

    with patch("time.sleep") as mock_sleep:
        passed, status = agent._wait_for_ci(123, timeout=5)

    assert passed is True
    assert status == "success"
    agent.github.get_ci_status.assert_not_called()
    mock_sleep.assert_not_called()


@pytest.mark.parametrize(
    ("module", "agent_name"),
    [
        (planner_main, "PlannerAgent"),
        (worker_main, "WorkerAgent"),
        (reviewer_main, "ReviewerAgent"),
    ],
)
def test_agent_cli_accepts_and_forwards_dry_run_option(
    module, agent_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each agent CLI should accept --dry-run and pass the mode into agent setup."""
    agent_cls = MagicMock()
    agent_instance = agent_cls.return_value
    agent_instance.run_once.return_value = True

    if agent_name == "PlannerAgent":
        agent_instance.create_spec.return_value = None

    monkeypatch.setattr(module, agent_name, agent_cls)
    monkeypatch.setattr(sys, "argv", ["prog", "owner/repo", "--once", "--dry-run=simulate-all"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 0
    assert agent_cls.call_args is not None
    assert agent_cls.call_args.kwargs.get("dry_run") == "simulate-all"
