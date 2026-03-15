"""Claude Code CLI-based intervention logic for the Orchestrator.

Called only when MonitorService detects an anomaly — never in the normal
monitoring loop — to keep claude invocations minimal.
Uses `claude -p` subprocess so no ANTHROPIC_API_KEY is required.
"""

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.monitor import Anomaly
from shared.github_client import GitHubClient

logger = logging.getLogger(__name__)

INTERVENTION_MODEL = "claude-sonnet-4-6"


class InterventionAction(Enum):
    RESET_SPEC = "reset_spec"  # Rewrite issue body to minimal focused spec
    STOP_WORKER = "stop_worker"  # Pause issue with orchestrator-paused label
    MARK_MANUAL = "mark_manual"  # Escalate to human (human-review label)
    CREATE_ISSUE = "create_issue"  # File a new status:ready issue for Worker to fix
    IGNORE = "ignore"  # False positive — no action


@dataclass
class InterventionPlan:
    """Decision produced by Claude for a given anomaly."""

    action: InterventionAction
    reason: str
    details: str
    anomaly: Anomaly
    new_spec: str | None = None  # populated for RESET_SPEC
    new_issue_title: str | None = None  # populated for CREATE_ISSUE
    new_issue_body: str | None = None  # populated for CREATE_ISSUE
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class InterventionService:
    """
    Uses Claude Code CLI subprocess to decide and execute interventions on anomalies.

    Design:
    - decide() asks Claude what to do (RESET_SPEC / STOP_WORKER / MARK_MANUAL / CREATE_ISSUE / IGNORE)
    - For RESET_SPEC, a second Claude call generates the minimal replacement spec
    - For CREATE_ISSUE, a second Claude call generates the title + spec body
    - execute() carries out the action and posts a GitHub comment
    """

    def __init__(
        self,
        github: GitHubClient,
        model: str = INTERVENTION_MODEL,
        claude_cli: str = "claude",
    ):
        self.github = github
        self.model = model
        self.claude_cli = claude_cli
        self._intervention_counts: dict[int, int] = {}  # issue_num → count
        self._decision_log: list[InterventionPlan] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def decide(self, anomaly: Anomaly) -> InterventionPlan:
        """Ask Claude to decide the best intervention for the anomaly."""
        context = self._build_context(anomaly)
        prompt = self._build_decision_prompt(context)

        logger.info(f"Asking Claude to decide intervention for {anomaly}")
        raw = self._call_claude(prompt)

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

        if plan.action == InterventionAction.CREATE_ISSUE:
            plan.new_issue_title, plan.new_issue_body = self._generate_issue_spec(
                anomaly
            )

        logger.info(f"Intervention decided: {plan.action.value} — {plan.reason}")
        self._decision_log.append(plan)
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
            case InterventionAction.CREATE_ISSUE:
                return self._do_create_issue(plan)
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

    def _do_create_issue(self, plan: InterventionPlan) -> bool:
        """Create a new GitHub Issue for the Worker to fix automatically."""
        title = plan.new_issue_title
        body = plan.new_issue_body
        if not title or not body:
            logger.error("CREATE_ISSUE called without title or body")
            return False

        issue_num = self.github.create_issue(
            title, body, labels=["status:ready", "orchestrator"]
        )
        if not issue_num:
            logger.error("Failed to create issue")
            return False

        logger.info(f"Created issue #{issue_num}: {title}")

        # If there's a source issue/PR, comment there to link
        source = plan.anomaly.issue_number or plan.anomaly.pr_number
        if source:
            comment = self._format_comment(
                plan,
                extra=f"Created issue #{issue_num} for automated fix. Worker will pick it up shortly.",
            )
            self.github.comment_issue(source, comment)

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
  Use when: spec is bloated, or repeated failures suggest the spec is unclear.
- **create_issue**: File a new GitHub Issue (status:ready) for the Worker to fix automatically.
  Use when: the anomaly reveals a concrete code-level bug or missing feature that can be implemented.
- **stop_worker**: Pause the issue with an orchestrator-paused label.
  Use when: the issue is consuming resources but cannot be fixed automatically.
- **mark_manual**: Flag for human review.
  Use when: the problem requires human judgement, or auto-intervention has failed multiple times.
- **ignore**: No action needed (false positive).
  Use when: the anomaly is transient or already resolving.

## Response Format
Respond with JSON only, no other text:
{{"action": "reset_spec|create_issue|stop_worker|mark_manual|ignore", "reason": "one sentence", "details": "brief explanation"}}"""

    def _generate_issue_spec(self, anomaly: Anomaly) -> tuple[str, str]:
        """Ask Claude to generate a title and spec body for a new fix issue."""
        prompt = f"""You are monitoring an AI workflow engine. An anomaly was detected that represents
a code-level problem that can be fixed automatically by a Worker agent.

## Anomaly
{self._build_context(anomaly)}

## Task
Write a GitHub Issue that describes the fix needed.
The Issue will be picked up by an automated Worker agent, so be precise and actionable.

Respond with JSON only:
{{"title": "short imperative title (max 60 chars)", "body": "full markdown spec with ## Overview, ## Requirements, ## Acceptance Criteria sections"}}"""

        raw = self._call_claude(prompt)
        try:
            data = self._extract_json(raw)
            return data.get(
                "title", "Fix: automated issue from orchestrator"
            ), data.get("body", raw)
        except (json.JSONDecodeError, KeyError):
            return "Fix: automated issue from orchestrator", raw

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

        return self._call_claude(prompt)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _call_claude(self, prompt: str, timeout: int = 120) -> str:
        """Call Claude Code CLI subprocess and return text output."""
        try:
            result = subprocess.run(
                [self.claude_cli, "-p", prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
            )
            if result.returncode != 0:
                logger.warning(
                    f"Claude CLI returned non-zero exit code {result.returncode}: {result.stderr[:200]}"
                )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error(f"Claude CLI timed out after {timeout}s")
            return ""
        except FileNotFoundError:
            logger.error(f"Claude CLI not found: {self.claude_cli}")
            return ""
        except Exception as e:
            logger.error(f"Claude CLI error: {e}")
            return ""

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

    def show_decisions(self) -> str:
        """Format the decision log as a human-readable table."""
        if not self._decision_log:
            return "No intervention decisions recorded."

        header = f"{'Time':<20} {'Target':<12} {'Action':<15} {'Reason'}"
        separator = "-" * 80
        lines = [header, separator]

        for plan in self._decision_log:
            ts = plan.decided_at.strftime("%Y-%m-%d %H:%M")
            target = (
                f"issue#{plan.anomaly.issue_number}"
                if plan.anomaly.issue_number
                else f"pr#{plan.anomaly.pr_number}"
                if plan.anomaly.pr_number
                else "system"
            )
            lines.append(f"{ts:<20} {target:<12} {plan.action.value:<15} {plan.reason}")

        return "\n".join(lines)
