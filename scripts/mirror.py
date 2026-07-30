#!/usr/bin/env python3
"""Discover and publish Helm charts from configured repositories to OCI."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cmp_to_key
from pathlib import Path
from typing import Any, Iterable


CONFIG_SCHEMA = 1
STATE_SCHEMA = 2
PLAN_SCHEMA = 1
ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
DESTINATION_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?$"
)
SEMVER_PATTERN = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class MirrorError(RuntimeError):
    """Raised for an invalid configuration, state, plan, or command result."""


@dataclass(frozen=True)
class Repository:
    id: str
    name: str
    url: str
    destination: str
    enabled: bool
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    initial_mode: str


@dataclass(frozen=True)
class Configuration:
    root: Path
    oci_root: str
    batch_size: int
    initial_mode: str
    repositories: tuple[Repository, ...]

    def state_path(self, repository: Repository) -> Path:
        return self.root / "state" / f"{repository.id}.json"

    def oci_repository(self, repository: Repository) -> str:
        return f"{self.oci_root}/{repository.destination}"


class CommandRunner:
    """Run external commands and return their standard output."""

    def run(
        self,
        arguments: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> str:
        print("+", " ".join(arguments), flush=True)
        try:
            result = subprocess.run(
                arguments,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=None,
                env=env,
            )
        except FileNotFoundError as error:
            raise MirrorError(f"Required executable not found: {arguments[0]}") from error
        except subprocess.CalledProcessError as error:
            raise MirrorError(
                f"Command failed with exit code {error.returncode}: "
                + " ".join(arguments)
            ) from error
        return result.stdout


def read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError as error:
        raise MirrorError(f"File does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise MirrorError(f"Invalid JSON in {path}: {error}") from error


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def require_mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MirrorError(f"{location} must be an object")
    return value


def require_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise MirrorError(f"{location} must be a non-empty string")
    return value


def require_patterns(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise MirrorError(f"{location} must be a non-empty array of patterns")
    patterns = tuple(require_string(item, f"{location}[]") for item in value)
    return patterns


def reject_unknown_keys(
    value: dict[str, Any], allowed: set[str], location: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise MirrorError(f"{location} has unknown field(s): {', '.join(unknown)}")


def load_configuration(path: Path) -> Configuration:
    raw = require_mapping(read_json(path), "configuration")
    reject_unknown_keys(raw, {"schema", "defaults", "repositories"}, "configuration")
    if raw.get("schema") != CONFIG_SCHEMA:
        raise MirrorError(f"configuration.schema must be {CONFIG_SCHEMA}")

    defaults = require_mapping(raw.get("defaults"), "configuration.defaults")
    reject_unknown_keys(
        defaults,
        {"oci_root", "initial_mode", "batch_size"},
        "configuration.defaults",
    )
    oci_root = require_string(
        defaults.get("oci_root"), "configuration.defaults.oci_root"
    ).rstrip("/")
    if not oci_root.startswith("oci://") or oci_root == "oci://":
        raise MirrorError("configuration.defaults.oci_root must be an OCI URL")
    initial_mode = require_string(
        defaults.get("initial_mode", "new"),
        "configuration.defaults.initial_mode",
    )
    if initial_mode not in {"new", "all"}:
        raise MirrorError("configuration.defaults.initial_mode must be new or all")
    batch_size = defaults.get("batch_size", 100)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise MirrorError(
            "configuration.defaults.batch_size must be a positive integer"
        )

    repository_values = raw.get("repositories")
    if not isinstance(repository_values, list) or not repository_values:
        raise MirrorError("configuration.repositories must be a non-empty array")

    repositories: list[Repository] = []
    seen_ids: set[str] = set()
    seen_destinations: set[str] = set()
    allowed_repository_keys = {
        "id",
        "name",
        "url",
        "destination",
        "enabled",
        "include",
        "exclude",
        "initial_mode",
    }
    for index, repository_value in enumerate(repository_values):
        location = f"configuration.repositories[{index}]"
        item = require_mapping(repository_value, location)
        reject_unknown_keys(item, allowed_repository_keys, location)
        repository_id = require_string(item.get("id"), f"{location}.id")
        if not ID_PATTERN.fullmatch(repository_id):
            raise MirrorError(
                f"{location}.id must contain only lowercase letters, numbers, "
                "dots, underscores, or hyphens"
            )
        if repository_id in seen_ids:
            raise MirrorError(f"Duplicate repository id: {repository_id}")
        seen_ids.add(repository_id)

        destination = require_string(
            item.get("destination", repository_id), f"{location}.destination"
        ).strip("/")
        if (
            not DESTINATION_PATTERN.fullmatch(destination)
            or ".." in destination.split("/")
        ):
            raise MirrorError(f"Invalid OCI destination: {destination}")
        if destination in seen_destinations:
            raise MirrorError(f"Duplicate OCI destination: {destination}")
        seen_destinations.add(destination)

        url = require_string(item.get("url"), f"{location}.url")
        if not url.startswith("https://"):
            raise MirrorError(f"{location}.url must use HTTPS")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise MirrorError(f"{location}.enabled must be a boolean")
        include = require_patterns(item.get("include", ["*"]), f"{location}.include")
        exclude_value = item.get("exclude", [])
        if not isinstance(exclude_value, list):
            raise MirrorError(f"{location}.exclude must be an array of patterns")
        exclude = tuple(
            require_string(pattern, f"{location}.exclude[]")
            for pattern in exclude_value
        )
        repository_mode = require_string(
            item.get("initial_mode", initial_mode), f"{location}.initial_mode"
        )
        if repository_mode not in {"new", "all"}:
            raise MirrorError(f"{location}.initial_mode must be new or all")

        repositories.append(
            Repository(
                id=repository_id,
                name=require_string(item.get("name"), f"{location}.name"),
                url=url,
                destination=destination,
                enabled=enabled,
                include=include,
                exclude=exclude,
                initial_mode=repository_mode,
            )
        )

    return Configuration(
        root=path.resolve().parent.parent,
        oci_root=oci_root,
        batch_size=batch_size,
        initial_mode=initial_mode,
        repositories=tuple(repositories),
    )


def empty_state() -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "initialized": False,
        "published": {},
        "skipped": [],
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    state = require_mapping(read_json(path), f"state {path}")
    if state.get("schema") != STATE_SCHEMA:
        raise MirrorError(f"{path}: state.schema must be {STATE_SCHEMA}")
    if not isinstance(state.get("initialized"), bool):
        raise MirrorError(f"{path}: state.initialized must be a boolean")
    published = state.get("published")
    if not isinstance(published, dict):
        raise MirrorError(f"{path}: state.published must be an object")
    for key, digest in published.items():
        if not isinstance(key, str) or not SHA256_PATTERN.fullmatch(str(digest)):
            raise MirrorError(f"{path}: invalid published release or digest: {key}")
    skipped = state.get("skipped")
    if (
        not isinstance(skipped, list)
        or any(not isinstance(key, str) for key in skipped)
        or len(skipped) != len(set(skipped))
    ):
        raise MirrorError(f"{path}: state.skipped must contain unique strings")
    overlap = set(published) & set(skipped)
    if overlap:
        raise MirrorError(
            f"{path}: release cannot be both published and skipped: {min(overlap)}"
        )
    return {
        "schema": STATE_SCHEMA,
        "initialized": state["initialized"],
        "published": dict(sorted(published.items())),
        "skipped": sorted(skipped),
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    normalized = {
        "schema": STATE_SCHEMA,
        "initialized": bool(state["initialized"]),
        "published": dict(sorted(state["published"].items())),
        "skipped": sorted(set(state["skipped"])),
    }
    write_json_atomic(path, normalized)


def release_key(release: dict[str, str]) -> str:
    return f"{release['chart']}@{release['version']}"


def parse_semver(value: str) -> tuple[int, int, int, tuple[str, ...] | None]:
    match = SEMVER_PATTERN.fullmatch(value)
    if not match:
        raise MirrorError(f"Invalid Helm chart SemVer: {value}")
    prerelease = match.group("prerelease")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        tuple(prerelease.split(".")) if prerelease is not None else None,
    )


def compare_semver(left: str, right: str) -> int:
    left_major, left_minor, left_patch, left_pre = parse_semver(left)
    right_major, right_minor, right_patch, right_pre = parse_semver(right)
    left_core = (left_major, left_minor, left_patch)
    right_core = (right_major, right_minor, right_patch)
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    if left_pre is None or right_pre is None:
        if left_pre is None and right_pre is None:
            return 0
        return 1 if left_pre is None else -1
    for left_identifier, right_identifier in zip(left_pre, right_pre):
        if left_identifier == right_identifier:
            continue
        left_numeric = left_identifier.isdigit()
        right_numeric = right_identifier.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_identifier) < int(right_identifier) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_identifier < right_identifier else 1
    if len(left_pre) == len(right_pre):
        return 0
    return -1 if len(left_pre) < len(right_pre) else 1


def compare_releases(left: dict[str, str], right: dict[str, str]) -> int:
    if left["chart"] != right["chart"]:
        return -1 if left["chart"] < right["chart"] else 1
    # Newest version first within each chart.
    return -compare_semver(left["version"], right["version"])


def chart_is_selected(chart: str, repository: Repository) -> bool:
    return any(fnmatch.fnmatchcase(chart, pattern) for pattern in repository.include) and (
        not any(fnmatch.fnmatchcase(chart, pattern) for pattern in repository.exclude)
    )


def normalize_upstream(
    items: Any, repository: Repository
) -> list[dict[str, str]]:
    if not isinstance(items, list):
        raise MirrorError(f"{repository.id}: Helm search output must be an array")
    releases: list[dict[str, str]] = []
    seen: set[str] = set()
    expected_prefix = f"{repository.id}/"
    for index, raw_item in enumerate(items):
        item = require_mapping(raw_item, f"{repository.id} upstream[{index}]")
        name = require_string(item.get("name"), f"{repository.id} upstream[{index}].name")
        if not name.startswith(expected_prefix):
            raise MirrorError(
                f"{repository.id}: unexpected Helm repository alias in {name}"
            )
        chart = name[len(expected_prefix) :]
        if not chart or "/" in chart or not chart_is_selected(chart, repository):
            continue
        version = require_string(
            item.get("version"), f"{repository.id} upstream[{index}].version"
        )
        parse_semver(version)
        release = {
            "chart": chart,
            "version": version,
            "app_version": str(item.get("app_version", "")),
        }
        key = release_key(release)
        if key not in seen:
            seen.add(key)
            releases.append(release)
    return sorted(releases, key=cmp_to_key(compare_releases))


def select_releases(
    releases: list[dict[str, str]],
    state: dict[str, Any],
    mode: str,
    batch_size: int,
) -> tuple[list[dict[str, str]], list[str], bool]:
    if mode not in {"new", "all"}:
        raise MirrorError(f"Unknown synchronization mode: {mode}")
    handled = set(state["published"]) | set(state["skipped"])
    pending = [release for release in releases if release_key(release) not in handled]

    if state["initialized"]:
        return pending[:batch_size], [], True

    if mode == "all":
        return pending[:batch_size], [], len(pending) <= batch_size

    latest_keys: set[str] = set()
    latest_charts: set[str] = set()
    for release in releases:
        if release["chart"] not in latest_charts:
            latest_charts.add(release["chart"])
            latest_keys.add(release_key(release))
    latest_pending = [
        release
        for release in releases
        if release_key(release) in latest_keys and release_key(release) not in handled
    ]
    selected = latest_pending[:batch_size]
    finalize = len(latest_pending) <= batch_size
    skipped = (
        sorted(
            release_key(release)
            for release in releases
            if release_key(release) not in latest_keys
            and release_key(release) not in handled
        )
        if finalize
        else []
    )
    return selected, skipped, finalize


def select_repositories(
    configuration: Configuration, selector: str
) -> list[Repository]:
    enabled = {repo.id: repo for repo in configuration.repositories if repo.enabled}
    if selector == "all":
        return list(enabled.values())
    requested = [part.strip() for part in selector.split(",") if part.strip()]
    if not requested:
        raise MirrorError("Repository selector cannot be empty")
    unknown = sorted(set(requested) - set(enabled))
    if unknown:
        raise MirrorError(
            "Unknown or disabled repository id(s): " + ", ".join(unknown)
        )
    return [enabled[repository_id] for repository_id in dict.fromkeys(requested)]


def helm_environment(directory: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["HELM_REPOSITORY_CONFIG"] = str(directory / "repositories.yaml")
    environment["HELM_REPOSITORY_CACHE"] = str(directory / "repository-cache")
    return environment


def fetch_upstream(
    repository: Repository, runner: CommandRunner, directory: Path
) -> Any:
    environment = helm_environment(directory)
    runner.run(
        ["helm", "repo", "add", repository.id, repository.url, "--force-update"],
        env=environment,
    )
    runner.run(["helm", "repo", "update", repository.id], env=environment)
    output = runner.run(
        [
            "helm",
            "search",
            "repo",
            repository.id,
            "--versions",
            "--output",
            "json",
        ],
        env=environment,
    )
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise MirrorError(
            f"{repository.id}: Helm returned invalid JSON: {error}"
        ) from error


def create_plan(
    configuration: Configuration,
    repositories: Iterable[Repository],
    *,
    mode_override: str,
    batch_size_override: int,
    runner: CommandRunner,
    upstream_files: dict[str, Path] | None = None,
) -> dict[str, Any]:
    plan_repositories: list[dict[str, Any]] = []
    upstream_files = upstream_files or {}
    with tempfile.TemporaryDirectory(prefix="helm-chart-discover-") as directory_name:
        directory = Path(directory_name)
        for repository in repositories:
            if repository.id in upstream_files:
                upstream = read_json(upstream_files[repository.id])
            else:
                upstream = fetch_upstream(repository, runner, directory / repository.id)
            releases = normalize_upstream(upstream, repository)
            state_path = configuration.state_path(repository)
            state = load_state(state_path)
            mode = (
                repository.initial_mode
                if mode_override == "config"
                else mode_override
            )
            batch_size = (
                configuration.batch_size
                if batch_size_override == 0
                else batch_size_override
            )
            selected, skipped, mark_initialized = select_releases(
                releases, state, mode, batch_size
            )
            print(
                f"{repository.id}: {len(releases)} upstream, "
                f"{len(selected)} selected, {len(state['published'])} published"
            )
            plan_repositories.append(
                {
                    "id": repository.id,
                    "url": repository.url,
                    "oci_repository": configuration.oci_repository(repository),
                    "state": str(state_path.relative_to(configuration.root)),
                    "mode": mode,
                    "mark_initialized": mark_initialized,
                    "skip_after_success": skipped,
                    "releases": selected,
                }
            )
    return {
        "schema": PLAN_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "repositories": plan_repositories,
    }


def validate_plan(
    plan: Any, configuration: Configuration
) -> list[dict[str, Any]]:
    plan_object = require_mapping(plan, "plan")
    if plan_object.get("schema") != PLAN_SCHEMA:
        raise MirrorError(f"plan.schema must be {PLAN_SCHEMA}")
    entries = plan_object.get("repositories")
    if not isinstance(entries, list):
        raise MirrorError("plan.repositories must be an array")
    configured = {repository.id: repository for repository in configuration.repositories}
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(entries):
        entry = require_mapping(raw_entry, f"plan.repositories[{index}]")
        repository_id = require_string(
            entry.get("id"), f"plan.repositories[{index}].id"
        )
        if repository_id in seen or repository_id not in configured:
            raise MirrorError(f"Invalid or duplicate repository in plan: {repository_id}")
        seen.add(repository_id)
        repository = configured[repository_id]
        expected_state = str(
            configuration.state_path(repository).relative_to(configuration.root)
        )
        expected_oci = configuration.oci_repository(repository)
        if entry.get("url") != repository.url:
            raise MirrorError(f"{repository_id}: plan URL differs from configuration")
        if entry.get("state") != expected_state:
            raise MirrorError(f"{repository_id}: plan state path is invalid")
        if entry.get("oci_repository") != expected_oci:
            raise MirrorError(
                f"{repository_id}: plan OCI repository differs from configuration"
            )
        if not isinstance(entry.get("mark_initialized"), bool):
            raise MirrorError(f"{repository_id}: invalid mark_initialized")
        skipped = entry.get("skip_after_success")
        if not isinstance(skipped, list) or any(
            not isinstance(key, str) for key in skipped
        ):
            raise MirrorError(f"{repository_id}: invalid skip_after_success")
        raw_releases = entry.get("releases")
        if not isinstance(raw_releases, list):
            raise MirrorError(f"{repository_id}: plan releases must be an array")
        upstream_releases: list[dict[str, str]] = []
        for release_index, raw_release in enumerate(raw_releases):
            release = require_mapping(
                raw_release,
                f"plan.repositories[{index}].releases[{release_index}]",
            )
            chart = require_string(
                release.get("chart"),
                f"plan.repositories[{index}].releases[{release_index}].chart",
            )
            version = require_string(
                release.get("version"),
                f"plan.repositories[{index}].releases[{release_index}].version",
            )
            upstream_releases.append(
                {
                    "name": f"{repository_id}/{chart}",
                    "version": version,
                    "app_version": str(release.get("app_version", "")),
                }
            )
        releases = normalize_upstream(
            upstream_releases,
            repository,
        )
        validated.append({**entry, "releases": releases})
    return validated


def parse_chart_metadata(output: str) -> tuple[str, str]:
    name_match = re.search(r"^name:\s*(.+?)\s*$", output, re.MULTILINE)
    version_match = re.search(r"^version:\s*(.+?)\s*$", output, re.MULTILINE)
    if not name_match or not version_match:
        raise MirrorError("Unable to read name/version from chart metadata")
    return (
        name_match.group(1).strip("'\""),
        version_match.group(1).strip("'\""),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def publish_plan(
    plan: Any,
    configuration: Configuration,
    *,
    runner: CommandRunner,
    dry_run: bool,
) -> int:
    entries = validate_plan(plan, configuration)
    published_count = 0
    for entry in entries:
        repository = next(
            item for item in configuration.repositories if item.id == entry["id"]
        )
        state_path = configuration.state_path(repository)
        state = load_state(state_path)
        pending = [
            release
            for release in entry["releases"]
            if release_key(release) not in state["published"]
            and release_key(release) not in state["skipped"]
        ]
        for release in entry["releases"]:
            if release not in pending:
                print(f"{repository.id}: already handled {release_key(release)}")

        with tempfile.TemporaryDirectory(
            prefix=f"helm-chart-{repository.id}-"
        ) as directory_name:
            work_directory = Path(directory_name)
            environment = helm_environment(work_directory / "helm")
            if pending and not dry_run:
                runner.run(
                    [
                        "helm",
                        "repo",
                        "add",
                        repository.id,
                        repository.url,
                        "--force-update",
                    ],
                    env=environment,
                )
                runner.run(
                    ["helm", "repo", "update", repository.id],
                    env=environment,
                )

            for release_index, release in enumerate(pending):
                key = release_key(release)
                print(
                    f"{'[dry-run] ' if dry_run else ''}"
                    f"Publishing {repository.id}/{key} to {entry['oci_repository']}"
                )
                if dry_run:
                    published_count += 1
                    continue
                directory = work_directory / f"release-{release_index}"
                directory.mkdir()
                runner.run(
                    [
                        "helm",
                        "pull",
                        f"{repository.id}/{release['chart']}",
                        "--version",
                        release["version"],
                        "--destination",
                        str(directory),
                    ],
                    env=environment,
                )
                archives = list(directory.glob("*.tgz"))
                if len(archives) != 1:
                    raise MirrorError(
                        f"{repository.id}/{key}: expected one chart archive, "
                        f"found {len(archives)}"
                    )
                archive = archives[0]
                metadata = runner.run(["helm", "show", "chart", str(archive)])
                actual_name, actual_version = parse_chart_metadata(metadata)
                if (
                    actual_name != release["chart"]
                    or actual_version != release["version"]
                ):
                    raise MirrorError(
                        f"Chart metadata mismatch for {repository.id}/{key}: "
                        f"got {actual_name}@{actual_version}"
                    )
                digest = sha256_file(archive)
                runner.run(["helm", "push", str(archive), entry["oci_repository"]])
                state["published"][key] = digest
                save_state(state_path, state)
                published_count += 1

        if not dry_run:
            state["skipped"] = sorted(
                set(state["skipped"]) | set(entry["skip_after_success"])
            )
            if entry["mark_initialized"]:
                state["initialized"] = True
            save_state(state_path, state)
    return published_count


def parse_upstream_files(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        repository_id, separator, path = value.partition("=")
        if not separator or not repository_id or not path:
            raise MirrorError("--upstream-file must use ID=PATH")
        if repository_id in result:
            raise MirrorError(f"Duplicate upstream file for {repository_id}")
        result[repository_id] = Path(path)
    return result


def write_github_output(path: Path | None, name: str, value: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def count_plan_releases(plan: dict[str, Any]) -> int:
    return sum(len(entry["releases"]) for entry in plan["repositories"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/repositories.json"),
        help="Repository configuration file",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.set_defaults(command_name="validate")

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--repository", default="all")
    plan_parser.add_argument(
        "--mode", choices=("config", "new", "all"), default="config"
    )
    plan_parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Per-repository limit; 0 uses the configured value",
    )
    plan_parser.add_argument(
        "--output", type=Path, default=Path(".tmp/plan.json")
    )
    plan_parser.add_argument(
        "--upstream-file",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="Use captured Helm search JSON instead of the network",
    )
    plan_parser.add_argument("--github-output", type=Path)
    plan_parser.set_defaults(command_name="plan")

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument(
        "--plan", type=Path, default=Path(".tmp/plan.json")
    )
    publish_parser.add_argument("--dry-run", action="store_true")
    publish_parser.set_defaults(command_name="publish")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        configuration = load_configuration(args.config)
        if args.command_name == "validate":
            for repository in configuration.repositories:
                load_state(configuration.state_path(repository))
            print(
                f"Configuration is valid: {len(configuration.repositories)} "
                "repository/repositories"
            )
            return 0

        runner = CommandRunner()
        if args.command_name == "plan":
            if args.batch_size < 0:
                raise MirrorError("--batch-size cannot be negative")
            repositories = select_repositories(
                configuration, args.repository
            )
            upstream_files = parse_upstream_files(args.upstream_file)
            unexpected_files = sorted(
                set(upstream_files) - {repository.id for repository in repositories}
            )
            if unexpected_files:
                raise MirrorError(
                    "Upstream file supplied for unselected repository: "
                    + ", ".join(unexpected_files)
                )
            plan = create_plan(
                configuration,
                repositories,
                mode_override=args.mode,
                batch_size_override=args.batch_size,
                runner=runner,
                upstream_files=upstream_files,
            )
            write_json_atomic(args.output, plan)
            count = count_plan_releases(plan)
            write_github_output(args.github_output, "count", str(count))
            print(f"Plan written to {args.output}: {count} release(s)")
            return 0

        plan = read_json(args.plan)
        count = publish_plan(
            plan, configuration, runner=runner, dry_run=args.dry_run
        )
        print(f"{'Would publish' if args.dry_run else 'Published'} {count} release(s)")
        return 0
    except MirrorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
