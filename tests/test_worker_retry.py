"""Tests for worker retry comment parsing."""

import sys
from pathlib import Path

repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "worker-agent"))

from main import WorkerAgent


def test_parse_retry_count_uses_max_marker():
    comments = [
        {"body": "No marker here"},
        {"body": "<!-- worker-retry-count:1 -->"},
        {"body": "Some text\n<!-- worker-retry-count:3 -->"},
        {"body": "<!-- worker-retry-count:2 -->"},
    ]

    assert WorkerAgent._parse_retry_count(comments) == 3


def test_build_retry_comment_includes_marker_and_pr():
    comment = WorkerAgent._build_retry_comment(2, 45)

    assert "Auto-retry #2" in comment
    assert "PR #45" in comment
    assert "<!-- worker-retry-count:2 -->" in comment
