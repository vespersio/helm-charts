# Helm chart OCI mirror

This repository mirrors configured upstream Helm repositories to a
source-namespaced GHCR OCI repository:

```text
oci://ghcr.io/vespersio/helm-charts/<source>/<chart>
```

Examples:

```bash
helm install external-dns \
  oci://ghcr.io/vespersio/helm-charts/external-dns/external-dns \
  --version 1.21.1

helm install grafana \
  oci://ghcr.io/vespersio/helm-charts/grafana/grafana \
  --version 10.5.15

helm install vault \
  oci://ghcr.io/vespersio/helm-charts/hashicorp/vault \
  --version 0.30.0

helm install unleash \
  oci://ghcr.io/vespersio/helm-charts/unleash/unleash \
  --version 5.6.7

helm install sentry \
  oci://ghcr.io/vespersio/helm-charts/sentry-kubernetes/sentry \
  --version 33.1.0

helm install harbor \
  oci://ghcr.io/vespersio/helm-charts/harbor/harbor \
  --version 1.19.2

helm install gitlab \
  oci://ghcr.io/vespersio/helm-charts/gitlab/gitlab \
  --version 10.2.1
```

The source namespace prevents collisions when two upstream repositories expose
charts with the same name.

## Repository configuration

Sources are declared in
[`config/repositories.json`](config/repositories.json). Adding a source does not
require editing the GitHub Actions workflow.

```json
{
  "id": "example",
  "name": "Example",
  "url": "https://charts.example.com",
  "destination": "example",
  "enabled": true,
  "include": ["*"],
  "exclude": ["deprecated-*"]
}
```

Fields:

- `id` is the stable source identifier, Helm alias, and state filename.
- `url` must be an HTTPS Helm repository.
- `destination` is appended to the configured OCI root.
- `include` and `exclude` are shell-style chart name patterns.
- `enabled` allows a source to be disabled without deleting its state.
- An optional per-source `initial_mode` overrides the default `new` or `all`.

Configuration is intentionally JSON so synchronization needs only Python's
standard library, Helm, and the tools already present on GitHub-hosted runners.
Unknown fields, duplicate ids/destinations, invalid URLs, and invalid state are
rejected before publication.

## Synchronization

The scheduled workflow runs daily at 03:17 UTC. A manual run accepts:

- `repository`: `all`, one repository id, or comma-separated ids;
- `mode`: `config`, `new`, or `all`;
- `batch_size`: per-source limit, where `0` uses the configuration;
- `dry_run`: build and display a plan without logging in or publishing.

Discovery and publication are separate steps. GHCR authentication is skipped
when the plan is empty or the run is a dry run.

Each successfully pushed archive is immediately checkpointed in its source
state file with the SHA-256 checksum of the original `.tgz`. If a later push
fails, the workflow still commits successful checkpoints and the next run
continues with the remaining releases. The archive is not unpacked or
repacked, and its chart name and version are verified before push. Transient
registry failures are retried four times with exponential backoff before the
run is marked as failed.

Chart versions are parsed and ordered using SemVer rules (including Helm's
commonly accepted `v` prefix) instead of depending on the order returned by
`helm search repo`.

## Initial backfill

This is a new OCI namespace and does not reuse state or packages from the old
repositories. The configured initial mode is `all`, with a default batch of 100
releases per source and run. Repeated scheduled or manual runs continue until
each state file has `"initialized": true`.

To backfill one source faster, run the workflow manually with, for example:

```text
repository = grafana
mode       = all
batch_size = 500
```

Choose a batch that fits within the workflow timeout and upstream/registry rate
limits.

## Local commands

Validate configuration and state:

```bash
python3 scripts/mirror.py validate
```

Build a real plan without publishing:

```bash
python3 scripts/mirror.py plan --repository all --mode config
python3 scripts/mirror.py publish --plan .tmp/plan.json --dry-run
```

Build an offline plan from captured `helm search --output json` results:

```bash
python3 scripts/mirror.py plan \
  --repository external-dns \
  --upstream-file external-dns=tests/fixtures/external-dns.json
```

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

## Public catalog

The static catalog is generated directly from `config/repositories.json` and
the successfully published releases recorded in `state/*.json`. It shows the
latest available version of every chart and does not query GHCR at page load.

Build and preview it locally:

```bash
python3 scripts/build_catalog.py --output _site
python3 -m http.server 8000 --directory _site
```

Then open <http://localhost:8000>. The `Publish catalog` workflow deploys the
same output to GitHub Pages after source changes and after every synchronization
workflow completes.

## GHCR permissions

The workflow uses `GITHUB_TOKEN` with `packages: write` and `contents: write`.
After the first push, verify that each GHCR package has the required public or
private visibility. GHCR package visibility is managed separately from the
GitHub repository.
