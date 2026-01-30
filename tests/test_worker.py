"""Tests for Worker Agent."""

import sys
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
