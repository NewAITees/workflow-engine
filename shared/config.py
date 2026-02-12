"""Configuration management for workflow agents."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AgentConfig:
    """Configuration for an agent."""

    repo: str
    poll_interval: int = 300  # 5 minutes
    work_dir: str | None = None
    llm_backend: str = "codex"  # "codex" or "claude"
    codex_cli: str = "codex"
    claude_cli: str = "claude"
    gh_cli: str = "gh"
    log_level: str = "INFO"
    auto_merge: bool = False  # Auto-merge approved PRs
    merge_method: str = "squash"  # "squash", "merge", or "rebase"
    stale_lock_timeout_minutes: int = 30  # Recover stale implementing locks
    coverage_target: int = 80  # Minimum coverage threshold for worker test runs

    def __post_init__(self) -> None:
        if self.work_dir is None:
            self.work_dir = str(Path.home() / ".workflow-engine" / "workspaces")
        else:
            # Expand ~ to home directory
            self.work_dir = str(Path(self.work_dir).expanduser())
        # Validate backend
        if self.llm_backend not in ("codex", "claude"):
            raise ValueError(
                f"Invalid llm_backend: {self.llm_backend}. Must be 'codex' or 'claude'"
            )
        # Validate merge_method
        if self.merge_method not in ("squash", "merge", "rebase"):
            raise ValueError(
                f"Invalid merge_method: {self.merge_method}. Must be 'squash', 'merge', or 'rebase'"
            )
        if self.stale_lock_timeout_minutes <= 0:
            raise ValueError("stale_lock_timeout_minutes must be a positive integer")
        if not (0 <= self.coverage_target <= 100):
            raise ValueError("coverage_target must be between 0 and 100")


@dataclass
class WorkflowConfig:
    """Full workflow configuration."""

    repositories: dict[str, AgentConfig] = field(default_factory=dict)


def load_config(config_path: str | None = None) -> WorkflowConfig:
    """Load configuration from YAML file."""
    if config_path is None:
        env_config = os.environ.get("WORKFLOW_CONFIG")
        if env_config:
            config_path = env_config
        else:
            cwd_config = Path.cwd() / "config" / "repos.yml"
            if cwd_config.exists():
                config_path = str(cwd_config)
            else:
                config_path = str(Path(__file__).parent.parent / "config" / "repos.yml")

    config_file = Path(config_path)
    if not config_file.exists():
        return WorkflowConfig()

    with open(config_file) as f:
        data = yaml.safe_load(f) or {}

    repos = {}
    for repo_config in data.get("repositories", []):
        name = repo_config.pop("name")
        repos[name] = AgentConfig(repo=name, **repo_config)

    return WorkflowConfig(repositories=repos)


def get_agent_config(repo: str, config_path: str | None = None) -> AgentConfig:
    """Get configuration for a specific repository."""
    config = load_config(config_path)

    if repo in config.repositories:
        return config.repositories[repo]

    # Return default config for unregistered repos
    return AgentConfig(repo=repo)
