# Architecture

## Components
- `planner-agent`: turns stories/escalations into issue specs.
- `worker-agent`: implements `status:ready` work and opens PRs.
- `reviewer-agent`: reviews `status:reviewing` PRs.
- `shared/config.py`: loads repos.yml and resolves runtime settings.
- `scripts/setup-repository.sh`: bootstraps labels/templates in target repositories.

## Configuration Resolution Order
1. CLI arguments (e.g., positional `owner/repo`, run-mode flags)
2. Environment variables (`TARGET_REPOS`, `RUN_MODE`, `MAX_CONCURRENCY`, `FAIL_FAST`, `GITHUB_TOKEN`)
3. `repos.yml` (`defaults` and `repositories`)

## Sequence: Single Repository
1. Resolve one repo target.
2. Run one agent instance.
3. Emit per-repo start/success/failure and duration logs.

## Sequence: Multi Repository
1. Resolve target repos from `TARGET_REPOS` or enabled entries in `repos.yml`.
2. Iterate repositories in one process invocation.
3. Apply failure strategy:
   - `FAIL_FAST=true`: stop at first failed repo.
   - `FAIL_FAST=false`: continue all and print final failed list summary.
