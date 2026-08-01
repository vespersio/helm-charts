import unittest
from pathlib import Path

from scripts import mirror


REPOSITORY = mirror.Repository(
    id="example",
    name="Example",
    url="https://charts.example.test",
    destination="example",
    enabled=True,
    include=("*",),
    exclude=("private-*",),
    initial_mode="all",
)


def release(chart: str, version: str):
    return {"chart": chart, "version": version, "app_version": ""}


class DiscoveryTests(unittest.TestCase):
    def test_unleash_fixture_contains_all_configured_charts(self):
        root = Path(__file__).parents[1]
        configuration = mirror.load_configuration(
            root / "config" / "repositories.json"
        )
        repository = next(
            item for item in configuration.repositories if item.id == "unleash"
        )
        upstream = mirror.read_json(root / "tests" / "fixtures" / "unleash.json")

        normalized = mirror.normalize_upstream(upstream, repository)

        self.assertEqual(
            {item["chart"] for item in normalized},
            {"unleash", "unleash-edge", "unleash-enterprise", "unleash-proxy"},
        )

    def test_semver_comparison_handles_numeric_and_prerelease_versions(self):
        versions = [
            "1.9.0",
            "v1.11.0",
            "1.10.0-rc.1",
            "1.10.0",
            "1.10.0-rc.10",
        ]
        releases = [release("chart", version) for version in versions]
        ordered = sorted(
            releases,
            key=mirror.cmp_to_key(mirror.compare_releases),
        )
        self.assertEqual(
            [item["version"] for item in ordered],
            [
                "v1.11.0",
                "1.10.0",
                "1.10.0-rc.10",
                "1.10.0-rc.1",
                "1.9.0",
            ],
        )

    def test_normalize_deduplicates_filters_and_sorts(self):
        upstream = [
            {"name": "example/main", "version": "1.9.0"},
            {"name": "example/private-token", "version": "9.0.0"},
            {"name": "example/main", "version": "1.10.0"},
            {"name": "example/main", "version": "1.10.0"},
        ]
        normalized = mirror.normalize_upstream(upstream, REPOSITORY)
        self.assertEqual(
            [mirror.release_key(item) for item in normalized],
            ["main@1.10.0", "main@1.9.0"],
        )

    def test_all_mode_is_batched_and_resumes_from_published_state(self):
        releases = [
            release("main", "3.0.0"),
            release("main", "2.0.0"),
            release("main", "1.0.0"),
        ]
        state = mirror.empty_state()
        state["published"]["main@3.0.0"] = "sha256:" + "a" * 64
        selected, skipped, initialized = mirror.select_releases(
            releases, state, "all", 1
        )
        self.assertEqual(
            [mirror.release_key(item) for item in selected], ["main@2.0.0"]
        )
        self.assertEqual(skipped, [])
        self.assertFalse(initialized)

    def test_new_mode_explicitly_selects_latest_and_baselines_history(self):
        releases = [
            release("main", "2.0.0"),
            release("main", "1.0.0"),
            release("other", "3.0.0"),
            release("other", "2.0.0"),
        ]
        selected, skipped, initialized = mirror.select_releases(
            releases, mirror.empty_state(), "new", 10
        )
        self.assertEqual(
            [mirror.release_key(item) for item in selected],
            ["main@2.0.0", "other@3.0.0"],
        )
        self.assertEqual(skipped, ["main@1.0.0", "other@2.0.0"])
        self.assertTrue(initialized)

    def test_initialized_state_publishes_every_new_release_regardless_of_mode(self):
        state = mirror.empty_state()
        state["initialized"] = True
        state["published"]["main@1.0.0"] = "sha256:" + "a" * 64
        releases = [release("main", "2.0.0"), release("main", "1.0.0")]
        selected, skipped, initialized = mirror.select_releases(
            releases, state, "new", 10
        )
        self.assertEqual(
            [mirror.release_key(item) for item in selected], ["main@2.0.0"]
        )
        self.assertEqual(skipped, [])
        self.assertTrue(initialized)


if __name__ == "__main__":
    unittest.main()
