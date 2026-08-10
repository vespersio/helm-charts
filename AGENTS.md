# AGENTS.md

## Scope

This repository mirrors upstream Helm charts into source-namespaced GHCR OCI repositories and builds a static catalog from the recorded mirror state.

## Repository map

- `scripts/mirror.py`: configuration validation, release discovery, planning, and publishing.
- `scripts/build_catalog.py`: static catalog generator.
- `config/repositories.json`: authoritative upstream repository configuration.
- `state/*.json`: committed publication checkpoints; keep each filename aligned with its repository `id`.
- `site/`: catalog source templates and assets.
- `_site/`: generated output; do not edit it by hand.
- `tests/`: standard-library `unittest` suite and offline Helm search fixtures.

## Development rules

- Keep the runtime compatible with Python 3.12 and prefer the standard library; the workflows install no Python dependencies.
- Preserve strict validation of configuration, state, and plan schemas. Add or update tests for behavior changes and invalid-input cases.
- Do not publish charts, log in to GHCR, or modify publication state unless the task explicitly requires it. Use fixtures or `--dry-run` for local checks.
- When changing catalog markup, styles, or scripts, edit `site/`, then regenerate `_site/`.
- Keep changes focused; do not commit temporary plans, caches, downloaded chart archives, or credentials.

## Validation

Run before finishing:

```bash
python3 scripts/mirror.py validate
python3 -m unittest discover -s tests -v
```

For catalog changes also run:

```bash
python3 scripts/build_catalog.py --output _site
```
