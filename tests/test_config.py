"""Tests for configuration parsing and validation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import AgentConfig


def test_merge_method_default_is_squash():
    config = AgentConfig(repo="owner/repo")
    assert config.merge_method == "squash"


def test_merge_method_invalid_raises():
    with pytest.raises(ValueError):
        AgentConfig(repo="owner/repo", merge_method="invalid")


def test_merge_method_non_string_raises():
    with pytest.raises(ValueError):
        AgentConfig(repo="owner/repo", merge_method=123)
