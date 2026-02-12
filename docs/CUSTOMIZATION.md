# Customization

## Config Extension Points
- `config/repos.yml`:
  - `defaults.run_mode`
  - `defaults.max_concurrency`
  - `defaults.fail_fast`
  - per-repo `enabled`, `labels_profile`, and agent settings

## Replacing Templates
- Source templates live in:
  - `templates/github/ISSUE_TEMPLATE/`
  - `templates/github/PULL_REQUEST_TEMPLATE.md`
  - `templates/AGENTS.md`
- After edits, re-run:
  - `scripts/setup-repository.sh --repo owner/repo`

## Modifying Label Definitions
- Edit `config/labels.yml` profiles.
- Keep label names stable where automation depends on them (`status:*`).
- Re-run setup to update/create labels idempotently.
