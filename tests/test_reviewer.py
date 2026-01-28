"""Tests for reviewer agent decision parsing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestReviewerDecisionParsing:
    """Tests for reviewer decision parsing logic."""

    def _parse_decision(self, output: str) -> bool:
        """Extract the parsing logic from reviewer agent."""
        approved = False
        for line in output.split("\n"):
            line_upper = line.strip().upper()
            if line_upper.startswith("DECISION:"):
                decision_value = line_upper.replace("DECISION:", "").strip()
                approved = decision_value == "APPROVE"
                break
        return approved

    def test_approve_exact_match(self):
        """Test exact APPROVE match."""
        output = """DECISION: APPROVE

SUMMARY:
Code looks good."""
        assert self._parse_decision(output) is True

    def test_changes_requested_exact_match(self):
        """Test exact CHANGES_REQUESTED match."""
        output = """DECISION: CHANGES_REQUESTED

SUMMARY:
Found some issues."""
        assert self._parse_decision(output) is False

    def test_approve_in_summary_not_matched(self):
        """Test that APPROVE in summary doesn't trigger approval."""
        output = """DECISION: CHANGES_REQUESTED

SUMMARY:
I would APPROVE this if the tests passed.

ISSUES:
- Tests are failing"""
        assert self._parse_decision(output) is False

    def test_approve_lowercase_normalized(self):
        """Test that lowercase/mixed case is normalized."""
        output = """decision: approve

SUMMARY:
Looks good."""
        assert self._parse_decision(output) is True

    def test_decision_with_extra_whitespace(self):
        """Test decision parsing with extra whitespace."""
        output = """DECISION:    APPROVE

SUMMARY:
Good."""
        assert self._parse_decision(output) is True

    def test_decision_not_first_line(self):
        """Test that DECISION doesn't have to be first line."""
        output = """## Review

DECISION: APPROVE

SUMMARY:
Code is clean."""
        assert self._parse_decision(output) is True

    def test_no_decision_line(self):
        """Test handling when no DECISION line exists."""
        output = """SUMMARY:
The code looks fine but I'll let someone else decide.

ISSUES:
None"""
        assert self._parse_decision(output) is False

    def test_empty_decision_value(self):
        """Test handling of empty decision value."""
        output = """DECISION:

SUMMARY:
Unclear."""
        assert self._parse_decision(output) is False

    def test_partial_approve_not_matched(self):
        """Test that partial matches like APPROVED don't work."""
        output = """DECISION: APPROVED

SUMMARY:
Good."""
        # "APPROVED" != "APPROVE", should not match
        assert self._parse_decision(output) is False

    def test_approve_with_comment_on_same_line(self):
        """Test that extra text after APPROVE causes failure."""
        output = """DECISION: APPROVE with minor suggestions

SUMMARY:
Good."""
        # "APPROVE with minor suggestions" != "APPROVE"
        assert self._parse_decision(output) is False

    def test_multiple_decision_lines_first_wins(self):
        """Test that first DECISION line wins."""
        output = """DECISION: CHANGES_REQUESTED

Actually wait...

DECISION: APPROVE

SUMMARY:
Changed my mind."""
        # First DECISION line should win
        assert self._parse_decision(output) is False
