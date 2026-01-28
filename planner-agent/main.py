#!/usr/bin/env python3
"""Planner Agent - Interactive specification generator.

Converts user stories into detailed specifications and creates
GitHub issues for the Worker Agent to implement.
"""

import argparse
import logging
import sys
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import get_agent_config
from shared.github_client import GitHubClient
from shared.llm_client import LLMClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("planner-agent")


class PlannerAgent:
    """Interactive agent for creating specifications from stories."""

    STATUS_READY = "status:ready"

    def __init__(self, repo: str, config_path: str | None = None):
        self.repo = repo
        self.config = get_agent_config(repo, config_path)
        self.github = GitHubClient(repo, gh_cli=self.config.gh_cli)
        self.llm = LLMClient(self.config)

        logger.info(f"Planner Agent initialized for {repo}")
        logger.info(f"LLM backend: {self.config.llm_backend}")

    def interactive_mode(self) -> None:
        """Run in interactive mode, converting stories to specs."""
        print(f"\n{'=' * 60}")
        print("Planner Agent - Interactive Mode")
        print(f"Repository: {self.repo}")
        print(f"LLM Backend: {self.config.llm_backend}")
        print(f"{'=' * 60}")
        print("\nEnter a user story to convert to a specification.")
        print("Type 'quit' or 'exit' to stop.\n")

        while True:
            try:
                print("-" * 40)
                story = self._get_multiline_input("📝 Story")

                if story.lower() in ("quit", "exit", "q"):
                    print("\nGoodbye!")
                    break

                if not story.strip():
                    print("Empty story, please try again.")
                    continue

                # Generate specification
                print(
                    f"\n🤔 Generating specification with {self.config.llm_backend}..."
                )
                spec = self._generate_spec(story)

                if not spec:
                    print("❌ Failed to generate specification")
                    continue

                # Display spec
                print("\n" + "=" * 40)
                print("📋 Generated Specification:")
                print("=" * 40)
                print(spec)
                print("=" * 40)

                # Confirm
                confirm = (
                    input("\n✅ Create issue with this spec? (y/n): ").strip().lower()
                )

                if confirm in ("y", "yes"):
                    title = self._extract_title(story, spec)
                    issue_num = self.github.create_issue(
                        title=title,
                        body=spec,
                        labels=[self.STATUS_READY],
                    )

                    if issue_num:
                        print(f"\n✅ Issue #{issue_num} created!")
                        print(f"   https://github.com/{self.repo}/issues/{issue_num}")
                    else:
                        print("\n❌ Failed to create issue")
                else:
                    print("\n❌ Cancelled")

            except KeyboardInterrupt:
                print("\n\nGoodbye!")
                break
            except Exception as e:
                logger.exception(f"Error: {e}")
                print(f"\n❌ Error: {e}")

    def create_spec(self, story: str) -> str | None:
        """Create a specification from a story (non-interactive)."""
        spec = self._generate_spec(story)
        if not spec:
            return None

        title = self._extract_title(story, spec)
        issue_num = self.github.create_issue(
            title=title,
            body=spec,
            labels=[self.STATUS_READY],
        )

        if issue_num:
            return f"Issue #{issue_num} created: https://github.com/{self.repo}/issues/{issue_num}"
        return None

    def _get_multiline_input(self, prompt: str) -> str:
        """Get potentially multiline input from user."""
        print(f"{prompt} (press Enter twice to finish):")
        lines = []
        empty_count = 0

        while True:
            try:
                line = input()
                if not line:
                    empty_count += 1
                    if empty_count >= 2:
                        break
                    lines.append("")
                else:
                    empty_count = 0
                    lines.append(line)
            except EOFError:
                break

        return "\n".join(lines).strip()

    def _generate_spec(self, story: str) -> str | None:
        """Generate a specification from a user story using LLM."""
        result = self.llm.create_spec(story)

        if not result.success:
            logger.error(f"LLM failed: {result.error}")
            return None

        return result.output.strip()

    def _extract_title(self, story: str, spec: str) -> str:
        """Extract a title from the story or spec."""
        # Try to get first line of story
        first_line = story.split("\n")[0].strip()

        # Clean up
        if first_line.startswith(("#", "-", "*")):
            first_line = first_line.lstrip("#-* ")

        # Truncate if too long
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."

        return first_line or "New Feature"


def main():
    parser = argparse.ArgumentParser(
        description="Planner Agent - Interactive specification generator"
    )
    parser.add_argument(
        "repo",
        help="Repository in owner/repo format",
    )
    parser.add_argument(
        "--config",
        "-c",
        help="Path to config file",
    )
    parser.add_argument(
        "--story",
        "-s",
        help="Story to convert (non-interactive mode)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    agent = PlannerAgent(args.repo, config_path=args.config)

    if args.story:
        # Non-interactive mode
        result = agent.create_spec(args.story)
        if result:
            print(result)
            sys.exit(0)
        else:
            print("Failed to create specification")
            sys.exit(1)
    else:
        # Interactive mode
        agent.interactive_mode()


if __name__ == "__main__":
    main()
