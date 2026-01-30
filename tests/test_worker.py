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
