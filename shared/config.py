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


@dataclass
class WorkflowConfig:
    """Full workflow configuration."""

    repositories: dict[str, AgentConfig] = field(default_factory=dict)


def load_config(config_path: str | None = None) -> WorkflowConfig:
    """Load configuration from YAML file."""
    if config_path is None:
        config_path = os.environ.get(
            "WORKFLOW_CONFIG",
            str(Path(__file__).parent.parent / "config" / "repos.yml"),
        )

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
