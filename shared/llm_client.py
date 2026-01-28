"""Unified LLM client supporting both Codex and Claude Code CLI."""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
