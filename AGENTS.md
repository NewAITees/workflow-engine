# Repository Guidelines

## Project Structure & Module Organization
- `shared/`: Core utilities shared by all agents (GitHub client, LLM client, locking).
- `planner-agent/`, `worker-agent/`, `reviewer-agent/`: Agent entry points and workflows.
- `config/`: Runtime configuration (`repos.yml`); copy from `config/repos.yml.example`.
- `tests/`: Pytest suite covering shared modules and agent helpers.
- `docs/`, `scripts/`: Supplemental documentation and tooling.

## Build, Test, and Development Commands
Use `uv` for dependency management and execution.

- `uv sync --all-extras`: Install runtime + dev dependencies.
- `uv run planner-agent/main.py owner/repo`: Run the planner agent.
- `uv run worker-agent/main.py owner/repo --once --verbose`: Run the worker once with debug.
- `uv run reviewer-agent/main.py owner/repo --once --verbose`: Run the reviewer once with debug.
- `uv run pytest`: Run the full test suite.
- `uv run ruff check .`: Lint (imports, style, naming).
- `uv run ruff format .`: Format code.
- `uv run mypy .`: Static type checks.

## Coding Style & Naming Conventions
- Python 3.11+, 4-space indentation.
- Formatting and linting via Ruff (`line-length = 88`).
- Type hints are required for functions (mypy `disallow_untyped_defs = true`).
- Module naming follows existing folders (`planner-agent`, `worker-agent`, `reviewer-agent`).
- Status labels use `status:<state>` (e.g., `status:ready`, `status:reviewing`).

## Testing Guidelines
- Framework: `pytest` with `pytest-asyncio` (`asyncio_mode = auto`).
- Tests live in `tests/` and use `test_*.py` naming.
- Run a single test file with `uv run pytest tests/test_lock.py -v`.
- Prefer mocking `gh` CLI calls to avoid hitting GitHub during tests.

## Commit & Pull Request Guidelines
- Git history is minimal; the only commit uses `feat: ...`. Keep commits short and descriptive,
  and prefer conventional prefixes like `feat:`, `fix:`, `chore:`, or `docs:` unless the team
  specifies otherwise.
- PRs should include: purpose, key changes, and any related issue links. If behavior changes,
  add test coverage and mention how you verified it.

## Configuration & Security Tips
- Copy `config/repos.yml.example` to `config/repos.yml` and keep secrets out of Git.
- Ensure `gh auth login` is configured before running agents.
