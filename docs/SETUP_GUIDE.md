# Setup Guide

## Prerequisites
- `gh` CLI installed and authenticated (`gh auth login`)
- Write permission for target repository (labels, contents, push)
- `uv` installed for local validation

## Quick Start
1. Copy configuration:
   - `cp config/repos.yml.example config/repos.yml`
2. Bootstrap target repository:
   - `scripts/setup-repository.sh --repo owner/repo`
3. Run one cycle:
   - `uv run worker-agent/main.py owner/repo --once`

## Dry Run
- `scripts/setup-repository.sh --repo owner/repo --dry-run`
- Output lists planned label/template operations only. No mutating GitHub API call is executed.

## Recovery
- Auth error:
  - `gh auth login`
  - Re-run setup command.
- Partial failure after clone/push:
  - Re-run `scripts/setup-repository.sh --repo owner/repo`
  - Script is idempotent for labels and template sync.
