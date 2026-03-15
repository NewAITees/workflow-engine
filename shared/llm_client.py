"""Unified LLM client supporting both Codex and Claude Code CLI."""

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AgentConfig

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    """Result of LLM invocation."""

    success: bool
    output: str
    error: str | None = None


class LLMClient:
    """Unified LLM client that supports both Codex and Claude Code CLI."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.backend = config.llm_backend

        if self.backend == "codex":
            self.cli = config.codex_cli
        else:
            self.cli = config.claude_cli

        logger.info(f"LLM Client initialized with backend: {self.backend} ({self.cli})")

    def _run(
        self,
        prompt: str,
        work_dir: Path | None = None,
        timeout: int = 600,
        allowed_tools: list[str] | None = None,
    ) -> LLMResult:
        """Run the LLM CLI with a prompt."""
        try:
            cmd = self._build_command(prompt, allowed_tools)

            logger.info(f"Running {self.backend} in {work_dir or 'current directory'}")
            logger.debug(f"Prompt length: {len(prompt)} chars")

            result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
            )

            if result.returncode != 0:
                return LLMResult(
                    success=False,
                    output=result.stdout,
                    error=result.stderr or f"Exit code: {result.returncode}",
                )

            return LLMResult(success=True, output=result.stdout)

        except subprocess.TimeoutExpired:
            return LLMResult(
                success=False,
                output="",
                error=f"{self.backend} timed out after {timeout}s",
            )
        except FileNotFoundError:
            return LLMResult(
                success=False,
                output="",
                error=f"{self.backend} CLI not found: {self.cli}",
            )
        except Exception as e:
            return LLMResult(
                success=False,
                output="",
                error=str(e),
            )

    def _build_command(
        self, prompt: str, allowed_tools: list[str] | None = None
    ) -> list[str]:
        """Build CLI command based on backend."""
        if self.backend == "codex":
            # Codex CLI command format - use 'exec' for non-interactive mode
            cmd = [self.cli, "exec", prompt]
            if allowed_tools:
                # Codex uses --full-auto for autonomous mode
                cmd.append("--full-auto")
            return cmd
        else:
            # Claude Code CLI command format
            cmd = [self.cli, "-p", prompt]
            if allowed_tools:
                cmd.extend(["--allowedTools", ",".join(allowed_tools)])
            return cmd

    def generate_implementation(
        self,
        spec: str,
        repo_context: str,
        work_dir: Path,
    ) -> LLMResult:
        """
        Generate implementation based on spec.

        Args:
            spec: The specification/requirements from the issue
            repo_context: Brief description of the repository
            work_dir: Working directory (cloned repo)

        Returns:
            LLMResult with success status and output
        """
        prompt = f"""You are implementing a feature for a software project.

## Repository Context
{repo_context}

## Specification
{spec}

## Instructions
1. Read the existing codebase to understand the structure and patterns
2. Implement the feature according to the specification
3. Follow existing code style and conventions
4. Add appropriate error handling
5. Keep the implementation minimal and focused

Do NOT:
- Add unnecessary features or "improvements"
- Create documentation files unless specified
- Over-engineer the solution

Start by exploring the codebase, then implement the changes."""

        return self._run(
            prompt,
            work_dir,
            allowed_tools=["Edit", "Write", "Read", "Glob", "Grep", "Bash"],
        )

    def generate_tests(
        self,
        spec: str,
        repo_context: str,
        work_dir: Path,
    ) -> LLMResult:
        """
        Generate pytest tests based on specification.

        TDD原則に従い、実装前にテストを生成する。

        Args:
            spec: The specification/requirements from the issue
            repo_context: Brief description of the repository
            work_dir: Working directory (cloned repo)

        Returns:
            LLMResult with success status and output
        """
        prompt = f"""You are writing tests for a feature BEFORE implementation (TDD).

## Repository Context
{repo_context}

## Specification
{spec}

## Instructions
1. Read the existing codebase to understand structure and patterns
2. Identify what needs to be tested based on the specification
3. Write pytest tests that will validate the implementation
4. Follow TDD principles: tests should be clear, specific, and minimal
5. Use existing test patterns if available (check tests/ directory)
6. Create test file in the appropriate location (typically tests/ directory)

Test Requirements:
- Use pytest framework
- Import necessary modules
- Create test classes/functions as appropriate
- Include edge cases mentioned in the spec
- Add docstrings explaining what each test validates

Do NOT:
- Implement the feature (only write tests)
- Over-complicate tests
- Add unnecessary dependencies

Start by exploring the codebase structure and existing tests."""

        return self._run(
            prompt,
            work_dir,
            allowed_tools=["Edit", "Write", "Read", "Glob", "Grep"],
        )

    def review_code(
        self,
        spec: str,
        diff: str,
        work_dir: Path | None = None,
    ) -> LLMResult:
        """
        Review code changes against spec.

        Args:
            spec: The original specification
            diff: The git diff to review
            work_dir: Optional working directory for context

        Returns:
            LLMResult with review comments
        """
        prompt = f"""You are reviewing a pull request implementation.

## Original Specification
{spec}

## Code Changes (diff)
```diff
{diff}
```

## Review Instructions
1. Check if the implementation matches the specification
2. Look for bugs, security issues, or logic errors
3. Verify error handling is appropriate
4. Check code style consistency

Provide your review in the following format:

### Decision
APPROVE or CHANGES_REQUESTED

### Summary
Brief summary of the changes

### Issues (if any)
- List of issues found

### Suggestions (optional)
- Optional improvements"""

        return self._run(prompt, work_dir, timeout=300)

    def review_code_with_severity(
        self,
        spec: str,
        diff: str,
        repo_context: str,
        work_dir: Path,
    ) -> LLMResult:
        """
        Review code and classify issues by severity.
        """
        prompt = f"""You are reviewing a pull request implementation.

## Specification
{spec}

## Code Changes (diff)
{diff}

## Repository Context
{repo_context}

## Instructions

1. Review the code changes against the specification
2. Review BOTH production code and test code in the diff
3. Validate test adequacy for changed behavior:
   - Missing tests for new/changed logic should be reported
   - Weak assertions or missing edge cases should be reported
   - Coverage gaps around critical paths should be reported as at least MAJOR
4. Identify issues and classify each by severity:
   - **CRITICAL**: Security vulnerabilities, data loss risks, crashes
   - **MAJOR**: Functional failures, performance problems
   - **MINOR**: Code style violations, refactoring recommendations
   - **TRIVIAL**: Typos, comments, documentation

5. Return a JSON response with this structure:
```json
{{
    "overall_decision": "approve or request_changes",
    "issues": [
        {{
            "severity": "critical|major|minor|trivial",
            "file": "path/to/file.py",
            "line": 42,
            "description": "Clear description of the issue",
            "suggestion": "How to fix it"
        }}
    ],
    "summary": "Brief overall assessment",
    "policy_candidates": [
        {{
            "title": "Short policy title",
            "why": "Why this recurring pattern matters",
            "trigger_tags": ["tag1", "tag2"],
            "trigger_conditions": ["Condition that indicates recurrence"],
            "rules": ["One concrete action sentence."],
            "strength": "low|medium|high"
        }}
    ]
}}
```
6. policy_candidates generation constraints:
   - Generate policy candidates ONLY when a recurring and reproducible pattern is evident.
   - Do NOT generate policy candidates for isolated or one-off issues.
   - Each item in rules[] MUST be a single concrete action sentence.
   - If no recurring pattern exists, return "policy_candidates": [].

## Decision Rules
- If ANY critical or major issues exist: overall_decision = "request_changes"
- If ONLY minor/trivial issues exist: overall_decision = "approve"
- If NO issues: overall_decision = "approve"

Start your review."""

        llm_result = self._run(
            prompt,
            work_dir,
            allowed_tools=["Read", "Grep", "Glob"],
        )

        if not llm_result.success:
            return llm_result

        parsed = self._extract_json_object(llm_result.output)
        normalized_result = {
            "overall_decision": parsed.get("overall_decision", "approve"),
            "issues": parsed.get("issues", []),
            "summary": parsed.get("summary", ""),
            "policy_candidates": self._normalize_policy_candidates(
                parsed.get("policy_candidates")
            ),
        }

        if normalized_result["overall_decision"] not in {"approve", "request_changes"}:
            normalized_result["overall_decision"] = "approve"
        if not isinstance(normalized_result["issues"], list):
            normalized_result["issues"] = []
        if not isinstance(normalized_result["summary"], str):
            normalized_result["summary"] = ""

        return LLMResult(
            success=True,
            output=json.dumps(normalized_result),
            error=llm_result.error,
        )

    def _extract_json_object(self, output: str) -> dict[str, Any]:
        """
        Extract and decode a JSON object from an LLM response.

        Args:
            output: Raw model output text.

        Returns:
            Decoded JSON object, or an empty object when parsing fails.
        """
        cleaned = output.strip()
        if not cleaned:
            return {}

        try:
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

        fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
        if fence_match:
            candidate = fence_match.group(1).strip()
            try:
                parsed = json.loads(candidate)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                pass

        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            return {}

        try:
            parsed = json.loads(cleaned[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _normalize_policy_candidates(self, value: Any) -> list[dict[str, Any]]:
        """
        Normalize policy_candidates into a deterministic list of candidate objects.

        Args:
            value: Raw policy_candidates value from model output.

        Returns:
            List of normalized policy candidate dictionaries.
        """
        if not isinstance(value, list):
            return []

        normalized_candidates: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue

            normalized_rules = self._normalize_string_list(item.get("rules"))
            if not normalized_rules:
                continue

            strength = item.get("strength")
            if strength not in {"low", "medium", "high"}:
                strength = "low"

            normalized_candidates.append(
                {
                    "title": item.get("title")
                    if isinstance(item.get("title"), str)
                    else "",
                    "why": item.get("why") if isinstance(item.get("why"), str) else "",
                    "trigger_tags": self._normalize_string_list(
                        item.get("trigger_tags")
                    ),
                    "trigger_conditions": self._normalize_string_list(
                        item.get("trigger_conditions")
                    ),
                    "rules": normalized_rules,
                    "strength": strength,
                }
            )

        return normalized_candidates

    def _normalize_string_list(self, value: Any) -> list[str]:
        """
        Normalize a value into a list containing only string elements.

        Args:
            value: Raw list-like value from model output.

        Returns:
            String-only list; non-list inputs become an empty list.
        """
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    def create_spec(self, story: str) -> LLMResult:
        """
        Convert a user story into a technical specification.

        Args:
            story: The user story to convert

        Returns:
            LLMResult with the specification
        """
        prompt = f"""Convert this user story into a detailed technical specification.

## User Story
{story}

## Output Format
Create a specification with these sections:

### Overview
Brief description of the feature

### Requirements
- Detailed list of functional requirements
- Each requirement should be testable

### Technical Details
- Implementation approach
- Files that may need changes
- Data structures or APIs involved

### Acceptance Criteria
- [ ] Checkboxes for each criterion
- Must be specific and verifiable

### Notes
- Edge cases to consider
- Potential issues or dependencies

Keep it concise but complete enough for implementation."""

        return self._run(prompt, timeout=300)
