#!/usr/bin/env python3
"""Build a static catalog from configured repositories and mirror state."""

from __future__ import annotations

import argparse
import html
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cmp_to_key
from pathlib import Path

try:
    from .mirror import (
        Configuration,
        MirrorError,
        Repository,
        compare_semver,
        load_configuration,
        load_state,
        parse_semver,
    )
except ImportError:  # pragma: no cover - used when invoked as a script
    from mirror import (  # type: ignore[no-redef]
        Configuration,
        MirrorError,
        Repository,
        compare_semver,
        load_configuration,
        load_state,
        parse_semver,
    )


TEMPLATE_TOKEN_PREFIX = "{{"


class CatalogError(RuntimeError):
    """Raised when catalog input or templates are invalid."""


@dataclass(frozen=True)
class Chart:
    name: str
    versions: tuple[str, ...]
    oci_url: str

    @property
    def latest_version(self) -> str:
        return self.versions[0]

    @property
    def version_count(self) -> int:
        return len(self.versions)


@dataclass(frozen=True)
class CatalogRepository:
    repository: Repository
    oci_repository: str
    initialized: bool
    published_count: int
    skipped_count: int
    charts: tuple[Chart, ...]

    @property
    def status(self) -> str:
        if not self.repository.enabled:
            return "paused"
        if not self.initialized:
            return "syncing"
        return "ready"


@dataclass(frozen=True)
class Catalog:
    generated_at: datetime
    repositories: tuple[CatalogRepository, ...]

    @property
    def chart_count(self) -> int:
        return sum(len(repository.charts) for repository in self.repositories)

    @property
    def version_count(self) -> int:
        return sum(
            repository.published_count for repository in self.repositories
        )


def parse_release_key(key: str) -> tuple[str, str]:
    chart, separator, version = key.rpartition("@")
    if not separator or not chart or not version:
        raise CatalogError(f"Invalid published release key: {key}")
    try:
        parse_semver(version)
    except MirrorError as error:
        raise CatalogError(f"Invalid published release key: {key}") from error
    return chart, version


def build_catalog(
    configuration: Configuration,
    *,
    generated_at: datetime | None = None,
) -> Catalog:
    repositories: list[CatalogRepository] = []
    for repository in configuration.repositories:
        state = load_state(configuration.state_path(repository))
        chart_versions: dict[str, list[str]] = {}
        for key in state["published"]:
            chart, version = parse_release_key(key)
            chart_versions.setdefault(chart, []).append(version)

        oci_repository = configuration.oci_repository(repository)
        charts: list[Chart] = []
        for chart_name, versions in sorted(chart_versions.items()):
            ordered_versions = sorted(
                versions,
                key=cmp_to_key(compare_semver),
                reverse=True,
            )
            charts.append(
                Chart(
                    name=chart_name,
                    versions=tuple(ordered_versions),
                    oci_url=f"{oci_repository}/{chart_name}",
                )
            )

        repositories.append(
            CatalogRepository(
                repository=repository,
                oci_repository=oci_repository,
                initialized=state["initialized"],
                published_count=len(state["published"]),
                skipped_count=len(state["skipped"]),
                charts=tuple(charts),
            )
        )

    timestamp = generated_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return Catalog(
        generated_at=timestamp.astimezone(UTC),
        repositories=tuple(repositories),
    )


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_status(repository: CatalogRepository) -> tuple[str, str]:
    labels = {
        "ready": ("Available", "status-ready"),
        "syncing": ("Initial sync", "status-syncing"),
        "paused": ("Updates paused", "status-paused"),
    }
    return labels[repository.status]


def install_command(chart: Chart, version: str) -> str:
    return f"helm install {chart.name} {chart.oci_url} --version {version}"


def render_version_history(chart: Chart) -> str:
    if chart.version_count < 2:
        return ""
    items: list[str] = []
    for index, version in enumerate(chart.versions):
        latest = (
            '<span class="history-latest">Latest</span>' if index == 0 else ""
        )
        items.append(
            f"""
            <li>
              <div class="history-version">
                <code>{escape(version)}</code>{latest}
              </div>
              <button
                class="button button-secondary"
                type="button"
                data-copy="{escape(install_command(chart, version))}"
                data-copy-label="Copy command"
                aria-label="Copy install command for {escape(chart.name)} version {escape(version)}"
              >Copy command</button>
            </li>"""
        )
    return f"""
        <details class="version-history">
          <summary>
            <span>View all {chart.version_count} versions</span>
            <span class="history-chevron" aria-hidden="true"></span>
          </summary>
          <ol>{''.join(items)}
          </ol>
        </details>"""


def render_chart_row(repository: CatalogRepository, chart: Chart) -> str:
    search_value = " ".join(
        (
            repository.repository.id,
            repository.repository.name,
            chart.name,
            *chart.versions,
            chart.oci_url,
        )
    ).lower()
    return f"""
      <li class="chart-row" data-chart-row data-search="{escape(search_value)}">
        <div class="chart-identity">
          <span class="chart-name">{escape(chart.name)}</span>
          <code class="chart-uri">{escape(chart.oci_url)}</code>
        </div>
        <span class="version-badge" aria-label="Latest version {escape(chart.latest_version)}">
          {escape(chart.latest_version)}
        </span>
        <div class="chart-actions">
          <button
            class="button button-secondary"
            type="button"
            data-copy="{escape(chart.oci_url)}"
            data-copy-label="Copy URI"
            aria-label="Copy OCI URI for {escape(chart.name)}"
          >Copy URI</button>
          <button
            class="button button-primary"
            type="button"
            data-copy="{escape(install_command(chart, chart.latest_version))}"
            data-copy-label="Copy command"
            aria-label="Copy install command for {escape(chart.name)}"
          >Copy command</button>
        </div>
        {render_version_history(chart)}
      </li>"""


def render_repository_card(repository: CatalogRepository) -> str:
    status_label, status_class = render_status(repository)
    repo = repository.repository
    search_value = " ".join(
        (repo.id, repo.name, repo.url, repository.oci_repository)
    ).lower()
    if repository.charts:
        rows = "\n".join(
            render_chart_row(repository, chart) for chart in repository.charts
        )
        chart_content = f"""
        <div class="chart-table-head" aria-hidden="true">
          <span>Chart</span><span>Latest</span><span>Actions</span>
        </div>
        <ul class="chart-list">{rows}
        </ul>"""
    else:
        message = (
            "No charts have been published yet. The first synchronization "
            "will make them appear here."
            if repository.status == "syncing"
            else "No published charts are recorded for this source."
        )
        chart_content = f"""
        <div class="repo-empty" data-empty-repository>
          <span class="empty-pulse" aria-hidden="true"></span>
          <div><strong>Nothing to install yet</strong><p>{escape(message)}</p></div>
        </div>"""

    noun = "chart" if len(repository.charts) == 1 else "charts"
    return f"""
    <article
      class="repo-card"
      data-repository="{escape(repo.id)}"
      data-search="{escape(search_value)}"
    >
      <header class="repo-header">
        <div>
          <div class="repo-title-line">
            <h2>{escape(repo.name)}</h2>
            <span class="status-pill {status_class}">
              <span class="status-dot" aria-hidden="true"></span>{status_label}
            </span>
          </div>
          <a class="upstream-link" href="{escape(repo.url)}" rel="noreferrer">
            {escape(repo.url)}<span aria-hidden="true"> ↗</span>
          </a>
        </div>
        <span class="repo-count">{len(repository.charts)} {noun}</span>
      </header>
      {chart_content}
    </article>"""


def render_catalog(catalog: Catalog, template: str) -> str:
    options = "\n".join(
        f'<option value="{escape(item.repository.id)}">'
        f"{escape(item.repository.name)}</option>"
        for item in catalog.repositories
    )
    cards = "\n".join(
        render_repository_card(repository)
        for repository in catalog.repositories
    )
    replacements = {
        "{{SOURCE_OPTIONS}}": options,
        "{{REPOSITORY_CARDS}}": cards,
        "{{SOURCE_COUNT}}": str(len(catalog.repositories)),
        "{{CHART_COUNT}}": str(catalog.chart_count),
        "{{VERSION_COUNT}}": str(catalog.version_count),
        "{{GENERATED_AT}}": catalog.generated_at.strftime("%Y-%m-%d %H:%M UTC"),
    }
    rendered = template
    for token, value in replacements.items():
        if token not in rendered:
            raise CatalogError(f"Template token is missing: {token}")
        rendered = rendered.replace(token, value)
    if TEMPLATE_TOKEN_PREFIX in rendered:
        raise CatalogError("Template contains an unreplaced token")
    return rendered


def build_site(
    *,
    config_path: Path,
    source_directory: Path,
    output_directory: Path,
    generated_at: datetime | None = None,
) -> Catalog:
    configuration = load_configuration(config_path)
    catalog = build_catalog(configuration, generated_at=generated_at)
    template_path = source_directory / "index.html"
    try:
        template = template_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise CatalogError(f"Template does not exist: {template_path}") from error

    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "index.html").write_text(
        render_catalog(catalog, template),
        encoding="utf-8",
    )
    (output_directory / ".nojekyll").touch()
    source_assets = source_directory / "assets"
    output_assets = output_directory / "assets"
    if output_assets.exists():
        shutil.rmtree(output_assets)
    shutil.copytree(source_assets, output_assets)
    return catalog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/repositories.json"),
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("site"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_site"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        catalog = build_site(
            config_path=args.config,
            source_directory=args.source,
            output_directory=args.output,
        )
    except (CatalogError, MirrorError) as error:
        print(f"error: {error}")
        return 1
    print(
        f"Catalog written to {args.output}: "
        f"{len(catalog.repositories)} sources, "
        f"{catalog.chart_count} charts, {catalog.version_count} versions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
