"""Tests for unified LLM client."""

import json
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

    @patch("subprocess.run")
    def test_generate_tests_success(self, mock_run):
        """Test successful test generation."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="# Generated tests\nimport pytest\n\ndef test_feature():\n    assert True",
            stderr="",
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        result = client.generate_tests(
            spec="Add hello() function",
            repo_context="Python project",
            work_dir=Path("/tmp/test"),
        )

        assert result.success is True
        assert "Generated tests" in result.output
        # Verify correct tools were used
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "codex" in cmd or "claude" in cmd

    @patch("subprocess.run")
    def test_generate_tests_failure(self, mock_run):
        """Test failed test generation."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Test generation failed",
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        result = client.generate_tests(
            spec="Add hello() function",
            repo_context="Python project",
            work_dir=Path("/tmp/test"),
        )

        assert result.success is False
        assert result.error is not None

    @patch("subprocess.run")
    def test_generate_tests_timeout(self, mock_run):
        """Test test generation timeout."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=600)

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        result = client.generate_tests(
            spec="Add hello() function",
            repo_context="Python project",
            work_dir=Path("/tmp/test"),
        )

        assert result.success is False
        assert "timed out" in result.error

    @patch.object(LLMClient, "_run")
    def test_review_prompt_includes_test_and_coverage_checks(self, mock_run):
        """Reviewer prompt should enforce test code and coverage gap checks."""
        mock_run.return_value = MagicMock(
            success=True,
            output='{"overall_decision":"approve","issues":[],"summary":"ok"}',
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        result = client.review_code_with_severity(
            spec="Implement feature X",
            diff="diff --git a/app.py b/app.py",
            repo_context="Repository: owner/repo",
            work_dir=Path("/tmp/test"),
        )

        assert result.success is True
        call_args = mock_run.call_args
        prompt = call_args[0][0]
        assert "Review BOTH production code and test code in the diff" in prompt
        assert (
            "Coverage gaps around critical paths should be reported as at least MAJOR"
            in prompt
        )
        assert '"policy_candidates"' in prompt
        assert (
            "Generate policy candidates ONLY when a recurring and reproducible pattern"
            in prompt
        )
        assert (
            "Do NOT generate policy candidates for isolated or one-off issues" in prompt
        )
        assert (
            "Each item in rules[] MUST be a single concrete action sentence" in prompt
        )

    @patch.object(LLMClient, "_run")
    def test_review_response_keeps_valid_policy_candidates(self, mock_run):
        """Valid policy candidates should be preserved after normalization."""
        mock_run.return_value = MagicMock(
            success=True,
            output=json.dumps(
                {
                    "overall_decision": "request_changes",
                    "issues": [{"severity": "major"}],
                    "summary": "Found recurring issue",
                    "policy_candidates": [
                        {
                            "title": "Require input validation tests",
                            "why": "Validation omissions recur",
                            "trigger_tags": ["tests", "validation"],
                            "trigger_conditions": ["Missing validation test in diff"],
                            "rules": [
                                "Add at least one failing and one passing validation test."
                            ],
                            "strength": "high",
                        }
                    ],
                }
            ),
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)

        result = client.review_code_with_severity(
            spec="Spec",
            diff="diff",
            repo_context="ctx",
            work_dir=Path("/tmp/test"),
        )
        payload = json.loads(result.output)

        assert payload["overall_decision"] == "request_changes"
        assert payload["issues"] == [{"severity": "major"}]
        assert payload["summary"] == "Found recurring issue"
        assert payload["policy_candidates"][0]["strength"] == "high"
        assert payload["policy_candidates"][0]["rules"] == [
            "Add at least one failing and one passing validation test."
        ]

    @patch.object(LLMClient, "_run")
    def test_review_response_accepts_explicit_empty_policy_candidates(self, mock_run):
        """Explicit empty policy_candidates should remain an empty list."""
        mock_run.return_value = MagicMock(
            success=True,
            output=json.dumps(
                {
                    "overall_decision": "approve",
                    "issues": [],
                    "summary": "No recurring patterns",
                    "policy_candidates": [],
                }
            ),
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)
        result = client.review_code_with_severity(
            "Spec", "diff", "ctx", Path("/tmp/test")
        )
        payload = json.loads(result.output)

        assert payload["policy_candidates"] == []

    @patch.object(LLMClient, "_run")
    def test_review_response_missing_policy_candidates_defaults_to_empty(
        self, mock_run
    ):
        """Missing policy_candidates should normalize to an empty list."""
        mock_run.return_value = MagicMock(
            success=True,
            output='{"overall_decision":"approve","issues":[],"summary":"ok"}',
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)
        result = client.review_code_with_severity(
            "Spec", "diff", "ctx", Path("/tmp/test")
        )
        payload = json.loads(result.output)

        assert payload["policy_candidates"] == []

    @patch.object(LLMClient, "_run")
    def test_review_response_null_policy_candidates_defaults_to_empty(self, mock_run):
        """Null policy_candidates should normalize to an empty list."""
        mock_run.return_value = MagicMock(
            success=True,
            output='{"overall_decision":"approve","issues":[],"summary":"ok","policy_candidates":null}',
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)
        result = client.review_code_with_severity(
            "Spec", "diff", "ctx", Path("/tmp/test")
        )
        payload = json.loads(result.output)

        assert payload["policy_candidates"] == []

    @patch.object(LLMClient, "_run")
    def test_review_response_wrong_policy_candidates_type_defaults_to_empty(
        self, mock_run
    ):
        """Non-list policy_candidates should normalize to an empty list."""
        mock_run.return_value = MagicMock(
            success=True,
            output='{"overall_decision":"approve","issues":[],"summary":"ok","policy_candidates":"bad"}',
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)
        result = client.review_code_with_severity(
            "Spec", "diff", "ctx", Path("/tmp/test")
        )
        payload = json.loads(result.output)

        assert payload["policy_candidates"] == []

    @patch.object(LLMClient, "_run")
    def test_review_response_mixed_policy_candidates_are_normalized(self, mock_run):
        """Mixed valid/invalid entries should normalize deterministically."""
        mock_run.return_value = MagicMock(
            success=True,
            output=json.dumps(
                {
                    "overall_decision": "approve",
                    "issues": [],
                    "summary": "ok",
                    "policy_candidates": [
                        "invalid",
                        {
                            "title": "Keep",
                            "why": "Recurring",
                            "trigger_tags": ["tag", 123],
                            "trigger_conditions": ["condition", {}],
                            "rules": ["Do this.", 7],
                            "strength": "medium",
                        },
                        {"title": "Drop because no rules", "rules": [1, 2]},
                    ],
                }
            ),
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)
        result = client.review_code_with_severity(
            "Spec", "diff", "ctx", Path("/tmp/test")
        )
        payload = json.loads(result.output)

        assert payload["policy_candidates"] == [
            {
                "title": "Keep",
                "why": "Recurring",
                "trigger_tags": ["tag"],
                "trigger_conditions": ["condition"],
                "rules": ["Do this."],
                "strength": "medium",
            }
        ]

    @patch.object(LLMClient, "_run")
    def test_review_response_invalid_fields_and_strength_are_defaulted(self, mock_run):
        """Invalid field types and invalid strength should use deterministic defaults."""
        mock_run.return_value = MagicMock(
            success=True,
            output=json.dumps(
                {
                    "overall_decision": "approve",
                    "issues": [],
                    "summary": "ok",
                    "policy_candidates": [
                        {
                            "title": 100,
                            "why": {"bad": "type"},
                            "trigger_tags": "bad",
                            "trigger_conditions": None,
                            "rules": ["Concrete action."],
                            "strength": "urgent",
                        }
                    ],
                }
            ),
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)
        result = client.review_code_with_severity(
            "Spec", "diff", "ctx", Path("/tmp/test")
        )
        payload = json.loads(result.output)

        assert payload["policy_candidates"] == [
            {
                "title": "",
                "why": "",
                "trigger_tags": [],
                "trigger_conditions": [],
                "rules": ["Concrete action."],
                "strength": "low",
            }
        ]

    @patch.object(LLMClient, "_run")
    def test_review_response_candidate_with_empty_rules_is_dropped(self, mock_run):
        """Candidates with empty normalized rules should be dropped."""
        mock_run.return_value = MagicMock(
            success=True,
            output=json.dumps(
                {
                    "overall_decision": "approve",
                    "issues": [],
                    "summary": "ok",
                    "policy_candidates": [
                        {
                            "title": "No usable rules",
                            "why": "Recurring",
                            "trigger_tags": ["tag"],
                            "trigger_conditions": ["condition"],
                            "rules": [1, {}, None],
                            "strength": "high",
                        }
                    ],
                }
            ),
        )

        config = AgentConfig(repo="owner/repo", llm_backend="codex")
        client = LLMClient(config)
        result = client.review_code_with_severity(
            "Spec", "diff", "ctx", Path("/tmp/test")
        )
        payload = json.loads(result.output)

        assert payload["policy_candidates"] == []
