"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.config import AgentConfig, WorkflowConfig, get_agent_config, load_config


def test_agent_config_expands_work_dir() -> None:
    home = Path.home()
    config = AgentConfig(repo="owner/repo", work_dir="~/workflows")

    assert Path(config.work_dir) == home / "workflows"


def test_agent_config_invalid_llm_backend() -> None:
    with pytest.raises(ValueError, match="Invalid llm_backend"):
        AgentConfig(repo="owner/repo", llm_backend="unknown")


def test_agent_config_invalid_merge_method() -> None:
    with pytest.raises(ValueError, match="Invalid merge_method"):
        AgentConfig(repo="owner/repo", merge_method="fast-forward")


def test_agent_config_invalid_stale_lock_timeout() -> None:
    with pytest.raises(ValueError, match="stale_lock_timeout_minutes"):
        AgentConfig(repo="owner/repo", stale_lock_timeout_minutes=0)


def test_load_config_missing_file(tmp_path: Path) -> None:
    config = load_config(str(tmp_path / "missing.yml"))

    assert isinstance(config, WorkflowConfig)
    assert config.repositories == {}


def test_load_config_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "repos.yml"
    config_path.write_text(
        """
repositories:
  - name: owner/repo
    poll_interval: 60
    work_dir: /tmp/workflow
    llm_backend: claude
    auto_merge: true
    merge_method: rebase
    stale_lock_timeout_minutes: 45
""",
        encoding="utf-8",
    )

    config = load_config(str(config_path))

    assert "owner/repo" in config.repositories
    repo_config = config.repositories["owner/repo"]
    assert repo_config.poll_interval == 60
    assert repo_config.work_dir == "/tmp/workflow"
    assert repo_config.llm_backend == "claude"
    assert repo_config.auto_merge is True
    assert repo_config.merge_method == "rebase"
    assert repo_config.stale_lock_timeout_minutes == 45


def test_get_agent_config_default(tmp_path: Path) -> None:
    config = get_agent_config("owner/repo", config_path=str(tmp_path / "missing.yml"))

    assert config.repo == "owner/repo"
