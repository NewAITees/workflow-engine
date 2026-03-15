"""Claude SDK-based intervention logic for the Orchestrator.

Called only when MonitorService detects an anomaly — never in the normal
monitoring loop — to keep API costs low.
"""

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.monitor import Anomaly
from shared.github_client import GitHubClient

logger = logging.getLogger(__name__)

INTERVENTION_MODEL = "claude-opus-4-6"
MAX_TOKENS = 1024


class InterventionAction(Enum):
    RESET_SPEC = "reset_spec"  # Rewrite issue body to minimal focused spec
    STOP_WORKER = "stop_worker"  # Pause issue with orchestrator-paused label
    MARK_MANUAL = "mark_manual"  # Escalate to human (human-review label)
    IGNORE = "ignore"  # False positive — no action


@dataclass
class InterventionPlan:
    """Decision produced by Claude for a given anomaly."""

    action: InterventionAction
    reason: str
    details: str
    anomaly: Anomaly
    new_spec: str | None = None  # populated for RESET_SPEC
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class InterventionService:
    """
    Uses Claude SDK to decide and execute interventions on anomalies.

    Design:
    - decide() asks Claude what to do (RESET_SPEC / STOP_WORKER / MARK_MANUAL / IGNORE)
    - For RESET_SPEC, a second Claude call generates the minimal replacement spec
    - execute() carries out the action and posts a GitHub comment
    """

    def __init__(self, github: GitHubClient, model: str = INTERVENTION_MODEL):
        self.github = github
        self.model = model
        self.client = anthropic.Anthropic()
        self._intervention_counts: dict[int, int] = {}  # issue_num → count

    # ── Public API ────────────────────────────────────────────────────────────

    def decide(self, anomaly: Anomaly) -> InterventionPlan:
        """Ask Claude to decide the best intervention for the anomaly."""
        context = self._build_context(anomaly)
        prompt = self._build_decision_prompt(context)

        logger.info(f"Asking Claude to decide intervention for {anomaly}")
        message = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        try:
            data = self._extract_json(raw)
            action = InterventionAction(data["action"])
            plan = InterventionPlan(
                action=action,
                reason=data.get("reason", ""),
                details=data.get("details", ""),
                anomaly=anomaly,
            )
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            logger.warning(
                f"Could not parse Claude response ({e}), defaulting to MARK_MANUAL"
            )
            plan = InterventionPlan(
                action=InterventionAction.MARK_MANUAL,
                reason="Claude response could not be parsed",
                details=raw[:500],
                anomaly=anomaly,
            )

        if plan.action == InterventionAction.RESET_SPEC and anomaly.issue_number:
            plan.new_spec = self._generate_minimal_spec(anomaly)

        logger.info(f"Intervention decided: {plan.action.value} — {plan.reason}")
        return plan

    def execute(self, plan: InterventionPlan) -> bool:
        """Carry out the intervention and post a GitHub comment."""
        match plan.action:
            case InterventionAction.RESET_SPEC:
                return self._do_reset_spec(plan)
            case InterventionAction.STOP_WORKER:
                return self._do_stop_worker(plan)
            case InterventionAction.MARK_MANUAL:
                return self._do_mark_manual(plan)
            case InterventionAction.IGNORE:
                logger.info(f"Ignoring anomaly: {plan.anomaly}")
                return True

    # ── Intervention actions ──────────────────────────────────────────────────

    def _do_reset_spec(self, plan: InterventionPlan) -> bool:
        issue_num = plan.anomaly.issue_number
        if not issue_num or not plan.new_spec:
            logger.error("RESET_SPEC called without issue_number or new_spec")
            return False

        ok = self.github.update_issue_body(issue_num, plan.new_spec)
        if not ok:
            logger.error(f"Failed to update issue #{issue_num} body")
            return False

        # Transition back to status:ready
        for label in ("status:failed", "status:ci-failed", "status:escalated"):
            self.github.remove_label(issue_num, label)
        self.github.add_label(issue_num, "status:ready")

        comment = self._format_comment(
            plan,
            extra=(
                "Spec has been rewritten to a minimal focused version. "
                "Issue transitioned back to `status:ready`."
            ),
        )
        self.github.comment_issue(issue_num, comment)
        self._increment_count(issue_num)
        return True

    def _do_stop_worker(self, plan: InterventionPlan) -> bool:
        issue_num = plan.anomaly.issue_number
        if not issue_num:
            return False

        self.github.add_label(issue_num, "orchestrator-paused")
        comment = self._format_comment(
            plan,
            extra="Issue paused with `orchestrator-paused` label. Remove to resume.",
        )
        self.github.comment_issue(issue_num, comment)
        self._increment_count(issue_num)
        return True

    def _do_mark_manual(self, plan: InterventionPlan) -> bool:
        issue_num = plan.anomaly.issue_number or plan.anomaly.pr_number
        if not issue_num:
            return False

        self.github.add_label(issue_num, "human-review")
        comment = self._format_comment(
            plan,
            extra=(
                "Escalated for human review. "
                "Remove `human-review` label after addressing to resume automation."
            ),
        )
        self.github.comment_issue(issue_num, comment)
        self._increment_count(issue_num)
        return True

    # ── Claude calls ─────────────────────────────────────────────────────────

    def _build_decision_prompt(self, context: str) -> str:
        return f"""You are monitoring an AI workflow engine that develops software autonomously.
An anomaly has been detected. Decide the best intervention.

## Anomaly Context
{context}

## Available Actions
- **reset_spec**: Rewrite the issue spec to be simpler and more focused.
  Use when: spec has bloated, or repeated failures suggest the spec is unclear.
- **stop_worker**: Pause the issue with an orchestrator-paused label.
  Use when: the issue is consuming resources but cannot be fixed automatically.
- **mark_manual**: Flag for human review.
  Use when: the problem requires human judgement, or auto-intervention has failed multiple times.
- **ignore**: No action needed (false positive).
  Use when: the anomaly is transient or already resolving.

## Response Format
Respond with JSON only, no other text:
{{"action": "reset_spec|stop_worker|mark_manual|ignore", "reason": "one sentence", "details": "brief explanation"}}"""

    def _generate_minimal_spec(self, anomaly: Anomaly) -> str:
        """Ask Claude to write a minimal replacement spec for the issue."""
        issue_num = anomaly.issue_number
        if not issue_num:
            return ""

        issue = self.github.get_issue(issue_num)
        if not issue:
            return ""

        prompt = f"""Rewrite the following GitHub issue body as a minimal, focused technical specification.
The original spec caused repeated implementation failures. Make it shorter, clearer, and achievable.

## Issue Title
{issue.title}

## Current Spec (may be bloated or unclear)
{issue.body[:3000]}

## Rules
- Keep it under 500 words
- Focus on the single most important requirement
- Use clear acceptance criteria with checkboxes
- Remove any scope beyond the original title's intent

Return the new spec text only (markdown), no preamble."""

        message = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_context(self, anomaly: Anomaly) -> str:
        lines = [
            f"Anomaly type: {anomaly.anomaly_type.value}",
            f"Detail: {anomaly.detail}",
            f"Detected at: {anomaly.detected_at.isoformat()}",
        ]
        if anomaly.issue_number:
            count = self._intervention_counts.get(anomaly.issue_number, 0)
            lines.append(f"Issue: #{anomaly.issue_number}")
            lines.append(f"Previous interventions on this issue: {count}")
        if anomaly.pr_number:
            lines.append(f"PR: #{anomaly.pr_number}")
        return "\n".join(lines)

    def _format_comment(self, plan: InterventionPlan, extra: str = "") -> str:
        lines = [
            "## 🤖 Orchestrator Intervention",
            "",
            f"**Action:** `{plan.action.value}`",
            f"**Anomaly:** {plan.anomaly.anomaly_type.value}",
            f"**Reason:** {plan.reason}",
        ]
        if plan.details:
            lines += ["**Details:**", plan.details]
        if extra:
            lines += ["", extra]
        return "\n".join(lines)

    def _extract_json(self, text: str) -> dict:
        """Extract JSON from Claude response, handling markdown code blocks."""
        if "```" in text:
            start = text.find("{", text.find("```"))
            end = text.rfind("}") + 1
            text = text[start:end]
        return json.loads(text)

    def _increment_count(self, issue_num: int) -> None:
        self._intervention_counts[issue_num] = (
            self._intervention_counts.get(issue_num, 0) + 1
        )

    def intervention_count(self, issue_num: int) -> int:
        return self._intervention_counts.get(issue_num, 0)
