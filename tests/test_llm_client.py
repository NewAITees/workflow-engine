"""Tests for unified LLM client."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import AgentConfig
from shared.llm_client import LLMClient


class TestLLMClient:
    """Tests for LLMClient."""

    def test_codex_backend_initialization(self):
        """Test initialization with codex backend."""
        config = AgentConfig(repo="owner/repo", llm_backend="codex", codex_cli="codex")
        client = LLMClient(config)

        assert client.backend == "codex"
        assert client.cli == "codex"

    def test_claude_backend_initialization(self):
        """Test initialization with claude backend."""
        config = AgentConfig(
            repo="owner/repo", llm_backend="claude", claude_cli="claude"
        )
        client = LLMClient(config)

        assert client.backend == "claude"
        assert client.cli == "claude"

    def test_codex_command_building(self):
        """Test command building for codex backend."""
        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        cmd = client._build_command("test prompt", allowed_tools=["Edit", "Write"])

        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "test prompt" in cmd
        assert "--full-auto" in cmd

    def test_codex_command_without_tools(self):
        """Test codex command building without tools."""
        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        cmd = client._build_command("test prompt", allowed_tools=None)

        assert cmd == ["codex", "exec", "test prompt"]
        assert "--full-auto" not in cmd

    def test_claude_command_building(self):
        """Test command building for claude backend."""
        config = AgentConfig(repo="owner/repo", llm_backend="claude")
        client = LLMClient(config)

        cmd = client._build_command("test prompt", allowed_tools=["Edit", "Write"])

        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "test prompt" in cmd
        assert "--allowedTools" in cmd
        assert "Edit,Write" in cmd

    def test_claude_command_without_tools(self):
        """Test claude command building without tools."""
        config = AgentConfig(repo="owner/repo", llm_backend="claude")
        client = LLMClient(config)

        cmd = client._build_command("test prompt", allowed_tools=None)

        assert cmd == ["claude", "-p", "test prompt"]
        assert "--allowedTools" not in cmd

    @patch("subprocess.run")
    def test_run_success(self, mock_run):
        """Test successful LLM invocation."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Generated code here",
            stderr="",
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        result = client._run("test prompt")

        assert result.success is True
        assert result.output == "Generated code here"
        assert result.error is None

    @patch("subprocess.run")
    def test_run_failure(self, mock_run):
        """Test failed LLM invocation."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error occurred",
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        result = client._run("test prompt")

        assert result.success is False
        assert result.error == "Error occurred"

    @patch("subprocess.run")
    def test_run_timeout(self, mock_run):
        """Test LLM timeout handling."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=600)

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        result = client._run("test prompt")

        assert result.success is False
        assert "timed out" in result.error

    @patch("subprocess.run")
    def test_run_cli_not_found(self, mock_run):
        """Test CLI not found handling."""
        mock_run.side_effect = FileNotFoundError()

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        result = client._run("test prompt")

        assert result.success is False
        assert "not found" in result.error

    def test_invalid_backend_raises(self):
        """Test that invalid backend raises ValueError."""
        try:
            AgentConfig(repo="owner/repo", llm_backend="invalid")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Invalid llm_backend" in str(e)

    def test_default_backend_is_codex(self):
        """Test that default backend is codex."""
        config = AgentConfig(repo="owner/repo")
        assert config.llm_backend == "codex"
