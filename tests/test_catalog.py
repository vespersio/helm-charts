import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from scripts import build_catalog
from scripts import mirror


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def configuration_data():
    return {
        "schema": 1,
        "defaults": {
            "oci_root": "oci://ghcr.io/example/helm-charts",
            "initial_mode": "all",
            "batch_size": 10,
        },
        "repositories": [
            {
                "id": "example",
                "name": "Example <Charts>",
                "url": "https://charts.example.test",
                "destination": "example",
                "enabled": True,
                "include": ["*"],
                "exclude": [],
            },
            {
                "id": "pending",
                "name": "Pending",
                "url": "https://pending.example.test",
                "destination": "pending",
                "enabled": True,
                "include": ["*"],
                "exclude": [],
            },
        ],
    }


class CatalogTests(unittest.TestCase):
    def write_inputs(self, root: Path) -> Path:
        config_path = root / "config" / "repositories.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps(configuration_data()), encoding="utf-8")
        state_path = root / "state" / "example.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "schema": 2,
                    "initialized": True,
                    "published": {
                        "widget@1.9.0": "sha256:" + "1" * 64,
                        "widget@1.10.0-rc.1": "sha256:" + "2" * 64,
                        "widget@1.10.0": "sha256:" + "3" * 64,
                    },
                    "skipped": [],
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def test_builds_latest_chart_versions_and_pending_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_inputs(Path(directory))
            configuration = mirror.load_configuration(config_path)
            catalog = build_catalog.build_catalog(
                configuration,
                generated_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
            )

            self.assertEqual(catalog.chart_count, 1)
            self.assertEqual(catalog.version_count, 3)
            self.assertEqual(
                catalog.repositories[0].charts[0].latest_version,
                "1.10.0",
            )
            self.assertEqual(
                catalog.repositories[0].charts[0].versions,
                ("1.10.0", "1.10.0-rc.1", "1.9.0"),
            )
            self.assertEqual(catalog.repositories[0].status, "ready")
            self.assertEqual(catalog.repositories[1].status, "syncing")

    def test_renders_static_site_and_escapes_configuration_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = self.write_inputs(root)
            output = root / "output"
            build_catalog.build_site(
                config_path=config_path,
                source_directory=PROJECT_ROOT / "site",
                output_directory=output,
                generated_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
            )

            page = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn("Example &lt;Charts&gt;", page)
            self.assertIn("--version 1.10.0", page)
            self.assertIn("--version 1.9.0", page)
            self.assertIn("View all 3 versions", page)
            self.assertNotIn("OCI namespace", page)
            self.assertIn("Initial sync", page)
            self.assertIn("2026-08-04 12:00 UTC", page)
            self.assertTrue((output / "assets" / "style.css").is_file())
            self.assertTrue((output / "assets" / "app.js").is_file())
            self.assertTrue((output / ".nojekyll").is_file())

    def test_rejects_malformed_release_keys(self):
        with self.assertRaisesRegex(
            build_catalog.CatalogError,
            "Invalid published release key",
        ):
            build_catalog.parse_release_key("missing-version")


if __name__ == "__main__":
    unittest.main()
