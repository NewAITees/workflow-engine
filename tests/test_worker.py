"""Tests for Worker Agent."""

import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import WorkerAgent class by loading the module directly
import importlib.util

spec = importlib.util.spec_from_file_location(
    "worker_main", Path(__file__).parent.parent / "worker-agent" / "main.py"
)
if spec is None or spec.loader is None:
    raise ImportError("Failed to load worker-agent/main.py")

worker_main = importlib.util.module_from_spec(spec)
sys.modules["worker_main"] = worker_main
spec.loader.exec_module(worker_main)
WorkerAgent = worker_main.WorkerAgent
from shared.github_client import Issue, PullRequest


class TestWorkerAgent:
    """Tests for WorkerAgent."""

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_agent_initialization(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Test that agent initializes with unique ID."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")

        assert agent.agent_id is not None
        assert agent.agent_id.startswith("worker-")
        assert len(agent.agent_id.split("-")[1]) == 8  # 8 hex chars

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_unique_agent_ids(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Test that each agent instance gets a unique ID."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent1 = WorkerAgent("owner/repo")
        agent2 = WorkerAgent("owner/repo")

        assert agent1.agent_id != agent2.agent_id

    @patch("subprocess.run")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_run_tests_success(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, mock_run
    ):
        """Test successful test execution."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="test_issue_123.py::test_feature PASSED",
            stderr="",
        )

        agent = WorkerAgent("owner/repo")
        agent.git = MagicMock()
        agent.git.path = Path("/tmp/test/repo")

        # Create mock test file
        with patch.object(Path, "exists", return_value=True):
            success, output = agent._run_tests(123)

        assert success is True
        assert "PASSED" in output
        mock_run.assert_called_once()

    @patch("subprocess.run")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_run_tests_failure(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, mock_run
    ):
        """Test failed test execution."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="test_issue_123.py::test_feature FAILED",
            stderr="AssertionError: expected True but got False",
        )

        agent = WorkerAgent("owner/repo")
        agent.git = MagicMock()
        agent.git.path = Path("/tmp/test/repo")

        with patch.object(Path, "exists", return_value=True):
            success, output = agent._run_tests(123)

        assert success is False
        assert "FAILED" in output

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_run_tests_file_not_found(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Test handling of missing test file."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")
        agent.git = MagicMock()
        agent.git.path = Path("/tmp/test/repo")

        with patch.object(Path, "exists", return_value=False):
            success, output = agent._run_tests(123)

        assert success is False
        assert "Test file not found" in output

    @patch("subprocess.run")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_run_tests_fallback_uses_single_issue_number_candidate(
        self,
        mock_config,
        mock_github,
        mock_lock,
        mock_llm,
        mock_git,
        mock_run,
        tmp_path,
    ):
        """When canonical file is missing, use a single matching test file."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        repo_path = tmp_path / "repo"
        tests_dir = repo_path / "tests"
        tests_dir.mkdir(parents=True)
        candidate = tests_dir / "test_stale_lock_issue_123.py"
        candidate.write_text("def test_x():\n    assert True\n")

        agent = WorkerAgent("owner/repo")
        agent.git = MagicMock()
        agent.git.path = repo_path

        success, _ = agent._run_tests(123)

        assert success is True
        run_args = mock_run.call_args[0][0]
        assert "tests/test_stale_lock_issue_123.py" in run_args

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_ensure_issue_test_file_moves_single_generated_file(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, tmp_path
    ):
        """Normalize a single generated test file to canonical issue test path."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        repo_path = tmp_path / "repo"
        tests_dir = repo_path / "tests"
        tests_dir.mkdir(parents=True)

        agent = WorkerAgent("owner/repo")
        before = agent._snapshot_test_files(repo_path)

        generated = tests_dir / "test_stale_lock.py"
        generated.write_text("def test_x():\n    assert True\n")

        agent._ensure_issue_test_file(7, repo_path, before)

        assert not generated.exists()
        assert (tests_dir / "test_issue_7.py").exists()

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_ensure_retry_tests_available_generates_and_commits_when_missing(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, tmp_path
    ):
        """Retry flow should regenerate tests when issue test file is missing."""
        from shared.llm_client import LLMResult

        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        repo_path = tmp_path / "repo"
        tests_dir = repo_path / "tests"
        tests_dir.mkdir(parents=True)

        agent = WorkerAgent("owner/repo")
        agent.git = MagicMock()
        agent.git.path = repo_path
        agent.git.commit = MagicMock(return_value=MagicMock(success=True, error=None))

        def _generate_tests_side_effect(*args, **kwargs):
            generated = tests_dir / "test_anything.py"
            generated.write_text("def test_x():\n    assert True\n")
            return LLMResult(success=True, output="ok")

        agent.llm.generate_tests = MagicMock(side_effect=_generate_tests_side_effect)

        issue = Issue(number=4, title="retry", body="spec body", labels=[])
        agent._ensure_retry_tests_available(issue, "feedback")

        agent.llm.generate_tests.assert_called_once()
        agent.git.commit.assert_called_once()
        assert (tests_dir / "test_issue_4.py").exists()

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_issue_workspace_does_not_fallback_on_body_exception(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Exception inside worktree usage should propagate without legacy fallback."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")
        agent.github = MagicMock()
        agent.github.get_default_branch.return_value = "main"
        agent.git.clone_or_pull = MagicMock()
        agent.git.create_branch = MagicMock()

        work_git = MagicMock()
        worktree_cm = MagicMock()
        worktree_cm.__enter__.return_value = work_git
        worktree_cm.__exit__.return_value = False
        agent.workspace_manager.worktree = MagicMock(return_value=worktree_cm)

        caught = None
        try:
            with agent._issue_workspace(14, "auto/issue-14") as issue_git:
                assert issue_git == work_git
                raise RuntimeError("boom")
        except RuntimeError as e:  # pragma: no cover - explicit assertion below
            caught = str(e)

        assert caught == "boom"
        agent.git.clone_or_pull.assert_not_called()
        agent.git.create_branch.assert_not_called()
        worktree_cm.__exit__.assert_called_once()
        assert worktree_cm.__exit__.call_args[0][0] is RuntimeError

    @patch("subprocess.run")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_run_tests_timeout(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, mock_run
    ):
        """Test test execution timeout."""
        import subprocess

        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["pytest"], timeout=300)

        agent = WorkerAgent("owner/repo")
        agent.git = MagicMock()
        agent.git.path = Path("/tmp/test/repo")

        with patch.object(Path, "exists", return_value=True):
            success, output = agent._run_tests(123)

        assert success is False
        assert "timed out" in output

    @patch("subprocess.run")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_run_tests_exception(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, mock_run
    ):
        """Test handling of unexpected exceptions."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        mock_run.side_effect = Exception("Unexpected error")

        agent = WorkerAgent("owner/repo")
        agent.git = MagicMock()
        agent.git.path = Path("/tmp/test/repo")

        with patch.object(Path, "exists", return_value=True):
            success, output = agent._run_tests(123)

        assert success is False
        assert "Test execution error" in output

    @patch("subprocess.run")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_run_quality_checks_success(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, mock_run
    ):
        """Quality checks should pass when both ruff and mypy succeed."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ruff ok", stderr=""),
            MagicMock(returncode=0, stdout="mypy ok", stderr=""),
        ]

        agent = WorkerAgent("owner/repo")
        agent.git = MagicMock()
        agent.git.path = Path("/tmp/test/repo")

        success, output = agent._run_quality_checks()

        assert success is True
        assert "ruff (exit=0)" in output
        assert "mypy (exit=0)" in output
        assert mock_run.call_count == 2

    @patch("subprocess.run")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_run_quality_checks_failure(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, mock_run
    ):
        """Quality checks should fail when any check fails."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="ruff error"),
            MagicMock(returncode=0, stdout="mypy ok", stderr=""),
        ]

        agent = WorkerAgent("owner/repo")
        agent.git = MagicMock()
        agent.git.path = Path("/tmp/test/repo")

        success, output = agent._run_quality_checks()

        assert success is False
        assert "ruff (exit=1)" in output

    @patch("time.sleep")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_wait_for_ci_success(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, mock_sleep
    ):
        """Test _wait_for_ci when CI passes."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        # Mock CI status: success
        mock_github_instance = mock_github.return_value
        mock_github_instance.get_ci_status.return_value = {
            "status": "success",
            "conclusion": "success",
            "checks": [],
            "pending_count": 0,
            "failed_count": 0,
        }

        agent = WorkerAgent("owner/repo")

        passed, status = agent._wait_for_ci(123, timeout=60)

        assert passed is True
        assert status == "success"

    @patch("time.sleep")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_wait_for_ci_failure(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, mock_sleep
    ):
        """Test _wait_for_ci when CI fails."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")

        # Mock CI status: failure
        agent.github.get_ci_status = MagicMock(
            return_value={
                "status": "failure",
                "conclusion": "failure",
                "checks": [],
                "pending_count": 0,
                "failed_count": 1,
            }
        )

        passed, status = agent._wait_for_ci(123, timeout=60)

        assert passed is False
        assert status == "failure"

    @patch("time.sleep")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_wait_for_ci_timeout(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, mock_sleep
    ):
        """Test _wait_for_ci timeout when CI stays pending."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")
        agent.CI_CHECK_INTERVAL = 1  # Speed up test

        # Mock CI status: always pending
        agent.github.get_ci_status = MagicMock(
            return_value={
                "status": "pending",
                "conclusion": "pending",
                "checks": [],
                "pending_count": 1,
                "failed_count": 0,
            }
        )

        passed, status = agent._wait_for_ci(123, timeout=3)

        assert passed is False
        assert status == "pending"

    @patch("time.sleep")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_wait_for_ci_none_grace_then_success(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git, mock_sleep
    ):
        """Treat persistent 'none' as no-CI only after grace period."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")
        agent.CI_CHECK_INTERVAL = 1
        agent.CI_NO_CHECKS_GRACE_SECONDS = 2
        agent.github.get_ci_status = MagicMock(
            return_value={
                "status": "none",
                "conclusion": "none",
                "checks": [],
                "pending_count": 0,
                "failed_count": 0,
            }
        )

        passed, status = agent._wait_for_ci(123, timeout=5)

        assert passed is True
        assert status == "success"

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_get_ci_failure_logs(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Test _get_ci_failure_logs formats logs correctly."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")

        # Mock CI logs
        agent.github.get_ci_logs = MagicMock(
            return_value=[
                {
                    "name": "test",
                    "conclusion": "failure",
                    "html_url": "https://example.com/test",
                    "output": {"title": "Test failed", "summary": "Error details here"},
                }
            ]
        )

        logs = agent._get_ci_failure_logs(123)

        assert "# CI Failure Report" in logs
        assert "test" in logs
        assert "Test failed" in logs
        assert "Error details here" in logs

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_get_ci_failure_logs_no_logs(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Test _get_ci_failure_logs when no logs available."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")

        # Mock no CI logs
        agent.github.get_ci_logs = MagicMock(return_value=[])

        logs = agent._get_ci_failure_logs(123)

        assert "no detailed logs available" in logs

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_ci_fix_loop_success_first_try(
        self,
        mock_config,
        mock_github,
        mock_lock,
        mock_llm,
        mock_git,
        mock_run,
        mock_sleep,
    ):
        """Test CI fix loop when fix succeeds on first try."""
        from shared.github_client import Issue
        from shared.llm_client import LLMResult

        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git_instance = mock_git.return_value
        mock_git_instance.workspace = "/tmp/test/workspace"
        mock_git_instance.path = Path("/tmp/test/repo")
        mock_git_instance.clone_or_pull.return_value = MagicMock(
            success=True, output=""
        )
        mock_git_instance.create_branch.return_value = MagicMock(
            success=True, output=""
        )
        mock_git_instance.commit.return_value = MagicMock(success=True, output="")
        mock_git_instance.push.return_value = MagicMock(success=True, output="")

        mock_llm_instance = mock_llm.return_value
        mock_llm_instance.generate_tests.return_value = LLMResult(
            success=True, output="tests generated"
        )
        mock_llm_instance.generate_implementation.return_value = LLMResult(
            success=True, output="implementation"
        )

        mock_lock_instance = mock_lock.return_value
        mock_lock_instance.try_lock_issue.return_value = MagicMock(success=True)

        # Mock test run success
        mock_run.return_value = MagicMock(returncode=0, stdout="test passed", stderr="")

        agent = WorkerAgent("owner/repo")
        agent.git = mock_git_instance
        agent.llm = mock_llm_instance
        agent.lock = mock_lock_instance
        agent.github.get_issue = MagicMock(
            return_value=Issue(
                number=123,
                title="Test issue",
                body="Test spec",
                labels=["status:ready"],
            )
        )
        agent.github.get_default_branch = MagicMock(return_value="main")
        agent.github.create_pr = MagicMock(
            return_value="https://github.com/owner/repo/pull/42"
        )
        agent.github.add_label = MagicMock(return_value=True)
        agent.github.remove_label = MagicMock(return_value=True)
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.comment_pr = MagicMock(return_value=True)
        agent.github.add_pr_label = MagicMock(return_value=True)
        agent.github.remove_pr_label = MagicMock(return_value=True)
        agent.github.get_ci_logs = MagicMock(return_value=[])

        # Mock CI: first check fails, after fix succeeds
        agent.github.get_ci_status = MagicMock(
            side_effect=[
                # First check: failure
                {
                    "status": "failure",
                    "conclusion": "failure",
                    "checks": [],
                    "pending_count": 0,
                    "failed_count": 1,
                },
                # After fix: success
                {
                    "status": "success",
                    "conclusion": "success",
                    "checks": [],
                    "pending_count": 0,
                    "failed_count": 0,
                },
            ]
        )
        # Override get_ci_logs with specific return value
        agent.github.get_ci_logs = MagicMock(
            return_value=[
                {
                    "name": "test",
                    "conclusion": "failure",
                    "html_url": "https://example.com/test",
                    "output": {"title": "Test failed", "summary": "Error"},
                }
            ]
        )

        with patch.object(Path, "exists", return_value=True):
            result = agent._try_process_issue(
                Issue(
                    number=123,
                    title="Test issue",
                    body="Test spec",
                    labels=["status:ready"],
                )
            )

        assert result is True
        # Should have called get_ci_status twice (initial + after fix)
        assert agent.github.get_ci_status.call_count == 2
        # Should have called get_ci_logs once
        assert agent.github.get_ci_logs.call_count == 1
        # Should have commented about CI fix
        assert any("CI" in str(call) for call in agent.github.comment_pr.call_args_list)

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_ci_fix_loop_failure_max_retries(
        self,
        mock_config,
        mock_github,
        mock_lock,
        mock_llm,
        mock_git,
        mock_run,
        mock_sleep,
    ):
        """Test CI fix loop when all retry attempts fail."""
        from shared.github_client import Issue
        from shared.llm_client import LLMResult

        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git_instance = mock_git.return_value
        mock_git_instance.workspace = "/tmp/test/workspace"
        mock_git_instance.path = Path("/tmp/test/repo")
        mock_git_instance.clone_or_pull.return_value = MagicMock(
            success=True, output=""
        )
        mock_git_instance.create_branch.return_value = MagicMock(
            success=True, output=""
        )
        mock_git_instance.commit.return_value = MagicMock(success=True, output="")
        mock_git_instance.push.return_value = MagicMock(success=True, output="")

        mock_llm_instance = mock_llm.return_value
        mock_llm_instance.generate_tests.return_value = LLMResult(
            success=True, output="tests generated"
        )
        mock_llm_instance.generate_implementation.return_value = LLMResult(
            success=True, output="implementation"
        )

        mock_lock_instance = mock_lock.return_value
        mock_lock_instance.try_lock_issue.return_value = MagicMock(success=True)

        # Mock test run success
        mock_run.return_value = MagicMock(returncode=0, stdout="test passed", stderr="")

        agent = WorkerAgent("owner/repo")
        agent.git = mock_git_instance
        agent.llm = mock_llm_instance
        agent.lock = mock_lock_instance
        agent.github.get_issue = MagicMock(
            return_value=Issue(
                number=123,
                title="Test issue",
                body="Test spec",
                labels=["status:ready"],
            )
        )
        agent.github.get_default_branch = MagicMock(return_value="main")
        agent.github.create_pr = MagicMock(
            return_value="https://github.com/owner/repo/pull/42"
        )
        agent.github.add_label = MagicMock(return_value=True)
        agent.github.remove_label = MagicMock(return_value=True)
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.comment_pr = MagicMock(return_value=True)
        agent.github.add_pr_label = MagicMock(return_value=True)
        agent.github.remove_pr_label = MagicMock(return_value=True)
        agent.github.get_ci_logs = MagicMock(return_value=[])

        # Mock CI: always fails
        agent.github.get_ci_status = MagicMock(
            return_value={
                "status": "failure",
                "conclusion": "failure",
                "checks": [],
                "pending_count": 0,
                "failed_count": 1,
            }
        )
        # Override get_ci_logs with specific return value
        agent.github.get_ci_logs = MagicMock(
            return_value=[
                {
                    "name": "test",
                    "conclusion": "failure",
                    "html_url": "https://example.com/test",
                    "output": {"title": "Test failed", "summary": "Error"},
                }
            ]
        )

        with patch.object(Path, "exists", return_value=True):
            result = agent._try_process_issue(
                Issue(
                    number=123,
                    title="Test issue",
                    body="Test spec",
                    labels=["status:ready"],
                )
            )

        assert result is False
        # Should have tried CI fix 3 times (MAX_CI_RETRIES)
        assert agent.github.get_ci_status.call_count == 1 + 3  # initial + 3 retries
        # Should have added ci-failed label
        assert any(
            "ci-failed" in str(call)
            for call in agent.github.add_pr_label.call_args_list
        )
        # CI test/check failures should also return PR to changes-requested
        assert any(
            "changes-requested" in str(call)
            for call in agent.github.add_pr_label.call_args_list
        )

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_ci_passes_immediately_no_fix_needed(
        self,
        mock_config,
        mock_github,
        mock_lock,
        mock_llm,
        mock_git,
        mock_run,
        mock_sleep,
    ):
        """Test when CI passes immediately, no fix loop triggered."""
        from shared.github_client import Issue
        from shared.llm_client import LLMResult

        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git_instance = mock_git.return_value
        mock_git_instance.workspace = "/tmp/test/workspace"
        mock_git_instance.path = Path("/tmp/test/repo")
        mock_git_instance.clone_or_pull.return_value = MagicMock(
            success=True, output=""
        )
        mock_git_instance.create_branch.return_value = MagicMock(
            success=True, output=""
        )
        mock_git_instance.commit.return_value = MagicMock(success=True, output="")
        mock_git_instance.push.return_value = MagicMock(success=True, output="")

        mock_llm_instance = mock_llm.return_value
        mock_llm_instance.generate_tests.return_value = LLMResult(
            success=True, output="tests generated"
        )
        mock_llm_instance.generate_implementation.return_value = LLMResult(
            success=True, output="implementation"
        )

        mock_lock_instance = mock_lock.return_value
        mock_lock_instance.try_lock_issue.return_value = MagicMock(success=True)

        # Mock test run success
        mock_run.return_value = MagicMock(returncode=0, stdout="test passed", stderr="")

        agent = WorkerAgent("owner/repo")
        agent.git = mock_git_instance
        agent.llm = mock_llm_instance
        agent.lock = mock_lock_instance
        agent.github.get_issue = MagicMock(
            return_value=Issue(
                number=123,
                title="Test issue",
                body="Test spec",
                labels=["status:ready"],
            )
        )
        agent.github.get_default_branch = MagicMock(return_value="main")
        agent.github.create_pr = MagicMock(
            return_value="https://github.com/owner/repo/pull/42"
        )
        agent.github.add_label = MagicMock(return_value=True)
        agent.github.remove_label = MagicMock(return_value=True)
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.comment_pr = MagicMock(return_value=True)
        agent.github.add_pr_label = MagicMock(return_value=True)
        agent.github.remove_pr_label = MagicMock(return_value=True)
        agent.github.get_ci_logs = MagicMock(return_value=[])

        # Mock CI: passes immediately
        agent.github.get_ci_status = MagicMock(
            return_value={
                "status": "success",
                "conclusion": "success",
                "checks": [],
                "pending_count": 0,
                "failed_count": 0,
            }
        )

        with patch.object(Path, "exists", return_value=True):
            result = agent._try_process_issue(
                Issue(
                    number=123,
                    title="Test issue",
                    body="Test spec",
                    labels=["status:ready"],
                )
            )

        assert result is True
        # Should have called get_ci_status only once (initial check)
        assert agent.github.get_ci_status.call_count == 1
        # Should NOT have called get_ci_logs
        assert agent.github.get_ci_logs.call_count == 0
        # Should NOT have added ci-failed label
        assert not any(
            "ci-failed" in str(call)
            for call in agent.github.add_pr_label.call_args_list
        )
        agent.github.add_label.assert_any_call(123, agent.STATUS_REVIEWING)
        # Issue implementation push should use force to avoid branch divergence failures
        mock_git_instance.push.assert_any_call("auto/issue-123", force=True)
        # Should comment local TDD result on PR
        assert any(
            "Local TDD validation passed" in str(call)
            for call in agent.github.comment_pr.call_args_list
        )

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_retry_exception_marks_failed_after_max_retries(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Retry exceptions should be counted and eventually stop auto-retry."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        git_instance = mock_git.return_value
        git_instance.workspace = "/tmp/test/workspace"
        git_instance.clone_or_pull.return_value = MagicMock(success=False, error="boom")

        lock_instance = mock_lock.return_value
        lock_instance.try_lock_pr.return_value = MagicMock(success=True)

        agent = WorkerAgent("owner/repo")
        agent.git = git_instance
        agent.lock = lock_instance
        agent.github.comment_pr = MagicMock(return_value=True)
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.remove_pr_label = MagicMock(return_value=True)
        agent.github.add_pr_label = MagicMock(return_value=True)
        agent.github.get_issue = MagicMock(
            return_value=Issue(
                number=123,
                title="Linked issue",
                body="Spec body",
                labels=[agent.STATUS_READY],
            )
        )

        # Simulate last allowed retry: next failure should mark as failed
        agent._get_retry_count = MagicMock(return_value=agent.MAX_RETRIES - 1)

        pr = PullRequest(
            number=42,
            title="Retry PR",
            body="Closes #123",
            labels=[agent.STATUS_CHANGES_REQUESTED],
            head_ref="auto/issue-123",
            base_ref="main",
        )

        result = agent._try_retry_pr(pr)

        assert result is False
        assert any(
            f"{agent.RETRY_MARKER}:{agent.MAX_RETRIES}" in str(call)
            for call in agent.github.comment_issue.call_args_list
        )
        agent.github.add_pr_label.assert_any_call(pr.number, agent.STATUS_FAILED)
        assert agent.github.comment_issue.call_count >= 1

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_process_stale_issue_lock_recovers_to_ready(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Stale issue implementing lock should be restored to ready."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
            stale_lock_timeout_minutes=30,
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")
        agent.github.list_issues = MagicMock(
            return_value=[
                Issue(
                    number=101,
                    title="Stale issue",
                    body="spec",
                    labels=[agent.STATUS_IMPLEMENTING],
                )
            ]
        )
        agent.github.list_prs = MagicMock(return_value=[])
        agent.github.get_issue_comments = MagicMock(
            return_value=[
                {
                    "body": "ACK:worker:worker-123:1",
                    "created_at": "2020-01-01T00:00:00Z",
                }
            ]
        )
        agent.github.remove_label = MagicMock(return_value=True)
        agent.github.add_label = MagicMock(return_value=True)
        agent.github.comment_issue = MagicMock(return_value=True)

        recovered = agent._process_stale_locks()

        assert recovered is True
        agent.github.remove_label.assert_called_once_with(
            101, agent.STATUS_IMPLEMENTING
        )
        agent.github.add_label.assert_called_once_with(101, agent.STATUS_READY)
        agent.github.comment_issue.assert_called_once()

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_process_stale_pr_lock_recovers_to_changes_requested(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Stale PR implementing lock should restore to changes-requested when reviewed."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
            stale_lock_timeout_minutes=30,
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")
        agent.github.list_issues = MagicMock(return_value=[])
        agent.github.list_prs = MagicMock(
            return_value=[
                PullRequest(
                    number=202,
                    title="Stale PR",
                    body="Closes #1",
                    labels=[agent.STATUS_IMPLEMENTING],
                    head_ref="auto/issue-1",
                    base_ref="main",
                )
            ]
        )
        agent.github.get_issue_comments = MagicMock(
            return_value=[
                {
                    "body": "ACK:worker:worker-123:1",
                    "created_at": "2020-01-01T00:00:00Z",
                }
            ]
        )
        agent.github.get_pr_reviews = MagicMock(
            return_value=[{"state": "CHANGES_REQUESTED"}]
        )
        agent.github.remove_pr_label = MagicMock(return_value=True)
        agent.github.add_pr_label = MagicMock(return_value=True)
        agent.github.comment_pr = MagicMock(return_value=True)

        recovered = agent._process_stale_locks()

        assert recovered is True
        agent.github.remove_pr_label.assert_called_once_with(
            202, agent.STATUS_IMPLEMENTING
        )
        agent.github.add_pr_label.assert_called_once_with(
            202, agent.STATUS_CHANGES_REQUESTED
        )
        agent.github.comment_pr.assert_called_once()

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_process_stale_lock_skips_recent_items(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Recent locks should not be recovered."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
            stale_lock_timeout_minutes=30,
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")
        agent.github.list_issues = MagicMock(
            return_value=[
                Issue(
                    number=303,
                    title="Recent issue",
                    body="spec",
                    labels=[agent.STATUS_IMPLEMENTING],
                )
            ]
        )
        agent.github.list_prs = MagicMock(return_value=[])
        agent.github.get_issue_comments = MagicMock(
            return_value=[
                {
                    "body": "ACK:worker:worker-123:1",
                    "created_at": "2100-01-01T00:00:00Z",
                }
            ]
        )
        agent.github.remove_label = MagicMock(return_value=True)
        agent.github.add_label = MagicMock(return_value=True)
        agent.github.comment_issue = MagicMock(return_value=True)

        recovered = agent._process_stale_locks()

        assert recovered is False
        agent.github.remove_label.assert_not_called()
        agent.github.add_label.assert_not_called()

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_try_process_issue_treats_no_changes_commit_as_no_op(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ) -> None:
        """No-op implementation commit should not be treated as hard failure."""
        from shared.llm_client import LLMResult

        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")
        agent.lock.try_lock_issue = MagicMock(return_value=MagicMock(success=True))
        agent.lock.mark_failed = MagicMock()
        agent._comment_worker_escalation = MagicMock()
        agent._snapshot_test_files = MagicMock(return_value=set())
        agent._ensure_issue_test_file = MagicMock(return_value=None)
        agent._issue_workspace = MagicMock(
            return_value=nullcontext(
                MagicMock(
                    path=Path("/tmp/test/repo"),
                    commit=MagicMock(
                        side_effect=[
                            MagicMock(success=True, output="tests committed"),
                            MagicMock(
                                success=False, output="", error="No changes to commit"
                            ),
                        ]
                    ),
                    push=MagicMock(success=True, output=""),
                )
            )
        )
        agent.llm.generate_tests = MagicMock(
            return_value=LLMResult(success=True, output="ok")
        )
        agent.llm.generate_implementation = MagicMock(
            return_value=LLMResult(success=True, output="ok")
        )
        agent._run_quality_checks = MagicMock(return_value=(True, "ok"))
        agent._run_tests = MagicMock(return_value=(True, "ok"))
        agent.github.remove_label = MagicMock(return_value=True)
        agent.github.add_label = MagicMock(return_value=True)
        agent.github.get_default_branch = MagicMock(return_value="main")
        agent.github.create_pr = MagicMock(
            return_value="https://github.com/owner/repo/pull/7"
        )
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.comment_pr = MagicMock(return_value=True)
        agent._wait_for_ci = MagicMock(return_value=(True, "success"))

        issue = Issue(
            number=33,
            title="No-op issue",
            body="Long enough spec " * 20,
            labels=[],
        )
        result = agent._try_process_issue(issue)

        assert result is False
        agent.lock.mark_failed.assert_not_called()
        agent._comment_worker_escalation.assert_not_called()

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_specification_unclear_detection(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Test specification unclear heuristics."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")

        assert agent._is_specification_unclear(
            "Missing requirement",
            "This specification is long enough to pass 100 characters. " * 2,
        )
        assert agent._is_specification_unclear("random failure", "short spec")
        assert not agent._is_specification_unclear(
            "random failure", "Detailed spec " * 20
        )

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_technical_failure_still_marked_failed(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Technical failures continue to mark issues as failed."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        git_instance = mock_git.return_value
        git_instance.clone_or_pull.return_value = MagicMock(success=False, error="boom")
        git_instance.cleanup_branch.return_value = MagicMock(success=True)

        agent = WorkerAgent("owner/repo")
        agent.git = git_instance
        agent._is_specification_unclear = MagicMock(return_value=False)
        agent.lock.mark_failed = MagicMock()
        agent.lock.mark_needs_clarification = MagicMock()
        agent.lock.try_lock_issue = MagicMock(return_value=MagicMock(success=True))

        issue = Issue(number=99, title="Title", body="Long enough spec" * 10, labels=[])

        assert agent._try_process_issue(issue) is False

        agent.lock.mark_failed.assert_called_once()
        agent.lock.mark_needs_clarification.assert_not_called()

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_planner_feedback_generation(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Planner feedback contains key sections."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")

        feedback = agent._generate_planner_feedback(
            issue_number=42,
            spec="Detailed specification text.",
            failure_reason="Test failed after multiple retries",
            attempt_count=2,
        )

        assert "Issue Number" in feedback
        assert "Failure Reason" in feedback
        assert "Original Specification" in feedback
        assert "Recommendations for Planner" in feedback

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_clarification_comment_posted(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Spec unclear failure triggers clarification workflow."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        git_instance = mock_git.return_value
        git_instance.clone_or_pull.return_value = MagicMock(success=False, error="boom")
        git_instance.cleanup_branch.return_value = MagicMock(success=True)

        agent = WorkerAgent("owner/repo")
        agent.git = git_instance
        agent._is_specification_unclear = MagicMock(return_value=True)
        agent._generate_planner_feedback = MagicMock(return_value="Feedback text")
        agent.lock.mark_needs_clarification = MagicMock()
        agent.lock.mark_failed = MagicMock()
        agent.lock.try_lock_issue = MagicMock(return_value=MagicMock(success=True))
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.add_label = MagicMock(return_value=True)
        agent.github.remove_label = MagicMock(return_value=True)

        issue = Issue(number=77, title="Spec issue", body="Too short", labels=[])

        result = agent._try_process_issue(issue)

        assert result is False
        agent.lock.mark_needs_clarification.assert_called_once()
        assert agent.github.comment_issue.call_count >= 2
        agent.github.add_label.assert_called_once_with(
            issue.number, agent.STATUS_NEEDS_CLARIFICATION
        )
        assert any(
            "ESCALATION:worker" in str(call)
            for call in agent.github.comment_issue.call_args_list
        )

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_test_retry_limit_triggers_worker_escalation(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """When test retries are exhausted, worker posts ESCALATION marker."""
        from shared.llm_client import LLMResult

        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")
        issue_git = MagicMock()
        issue_git.path = Path("/tmp/test/repo")
        issue_git.commit.return_value = MagicMock(success=True, output="")
        issue_git.push.return_value = MagicMock(success=True, output="")

        agent._issue_workspace = MagicMock(return_value=nullcontext(issue_git))
        agent._run_tests = MagicMock(
            side_effect=[
                (False, "fail 1"),
                (False, "fail 2"),
                (False, "fail 3"),
            ]
        )
        agent._run_quality_checks = MagicMock(return_value=(True, "quality ok"))
        agent.llm.generate_tests = MagicMock(
            return_value=LLMResult(success=True, output="tests generated")
        )
        agent.llm.generate_implementation = MagicMock(
            return_value=LLMResult(success=True, output="impl")
        )
        agent.lock.try_lock_issue = MagicMock(return_value=MagicMock(success=True))
        agent.lock.mark_failed = MagicMock()
        agent.lock.mark_needs_clarification = MagicMock()
        agent.git.cleanup_branch = MagicMock()
        agent.github.comment_issue = MagicMock(return_value=True)
        agent.github.add_label = MagicMock(return_value=True)
        agent.github.remove_label = MagicMock(return_value=True)

        issue = Issue(
            number=88, title="Retry issue", body="Long enough spec " * 20, labels=[]
        )
        result = agent._try_process_issue(issue)

        assert result is False
        agent.github.add_label.assert_any_call(
            issue.number, agent.STATUS_NEEDS_CLARIFICATION
        )
        assert any(
            "ESCALATION:worker" in str(call)
            for call in agent.github.comment_issue.call_args_list
        )

    @patch("shared.git_operations.GitOperations")
    @patch("shared.llm_client.LLMClient")
    @patch("shared.lock.LockManager")
    @patch("shared.github_client.GitHubClient")
    @patch("shared.config.get_agent_config")
    def test_short_spec_triggers_clarification(
        self, mock_config, mock_github, mock_lock, mock_llm, mock_git
    ):
        """Specifications shorter than threshold trigger clarification."""
        mock_config.return_value = MagicMock(
            work_dir="/tmp/test",
            llm_backend="codex",
            gh_cli="gh",
        )
        mock_git.return_value.workspace = "/tmp/test/workspace"

        agent = WorkerAgent("owner/repo")

        assert agent._is_specification_unclear("reason", "short spec")
