# AGENTS.md

## Scope

This repository mirrors upstream Helm charts into source-namespaced GHCR OCI repositories and builds a static catalog from the recorded mirror state.

## Repository map

- `scripts/mirror.py`: configuration validation, release discovery, planning, and publishing.
- `scripts/build_catalog.py`: static catalog generator.
- `config/repositories.json`: authoritative upstream repository configuration.
- `state/*.json`: committed publication checkpoints; keep each filename aligned with its repository `id`.
- `site/`: catalog source templates and assets.
- `_site/`: generated output built and published by the GitHub Pages workflow; do not edit or commit it locally.
- `tests/`: standard-library `unittest` suite and offline Helm search fixtures.

## Development rules

- Keep the runtime compatible with Python 3.12 and prefer the standard library; the workflows install no Python dependencies.
- Preserve strict validation of configuration, state, and plan schemas. Add or update tests for behavior changes and invalid-input cases.
- When adding a repository, also create `state/<id>.json` with schema 2, `initialized` set to `false`, an empty `published` object, and an empty `skipped` array; keep `<id>` aligned with the repository `id`.
- Do not publish charts, log in to GHCR, or modify publication state unless the task explicitly requires it. Use fixtures or `--dry-run` for local checks.
- When changing catalog markup, styles, scripts, configuration, or state, edit the source files only. CI regenerates `_site/` before publishing GitHub Pages.
- Keep changes focused; do not commit temporary plans, caches, downloaded chart archives, or credentials.

## Validation

Run before finishing:

```bash
python3 scripts/mirror.py validate
python3 -m unittest discover -s tests -v
```

For catalog changes, verify generation in a temporary directory outside the worktree so `_site/` remains untouched:

```bash
catalog_output="$(mktemp -d)"
python3 scripts/build_catalog.py --output "$catalog_output"
```
