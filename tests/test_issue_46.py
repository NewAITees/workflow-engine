"""Regression tests for issue #46 policy_candidates contract."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import AgentConfig
from shared.llm_client import LLMClient


@patch.object(LLMClient, "_run")
def test_issue_46_policy_candidates_key_is_always_present(mock_run):
    """review_code_with_severity should always include policy_candidates."""
    mock_run.return_value = MagicMock(
        success=True,
        output='{"overall_decision":"approve","issues":[],"summary":"ok"}',
    )

    client = LLMClient(AgentConfig(repo="owner/repo", llm_backend="codex"))
    result = client.review_code_with_severity(
        spec="spec",
        diff="diff",
        repo_context="ctx",
        work_dir=Path("/tmp/test"),
    )

    payload = json.loads(result.output)
    assert "policy_candidates" in payload
    assert payload["policy_candidates"] == []
