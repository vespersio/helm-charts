import json
import tempfile
import unittest
from pathlib import Path

from scripts import mirror


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
                "name": "Example",
                "url": "https://charts.example.test",
                "destination": "example",
                "enabled": True,
                "include": ["*"],
                "exclude": [],
            }
        ],
    }


class ConfigurationTests(unittest.TestCase):
    def write_configuration(self, root: Path, data: dict) -> Path:
        path = root / "config" / "repositories.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_loads_valid_configuration_and_derives_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = mirror.load_configuration(
                self.write_configuration(root, configuration_data())
            )
            repository = config.repositories[0]
            self.assertEqual(
                config.state_path(repository),
                root.resolve() / "state/example.json",
            )
            self.assertEqual(
                config.oci_repository(repository),
                "oci://ghcr.io/example/helm-charts/example",
            )

    def test_rejects_duplicate_ids(self):
        data = configuration_data()
        data["repositories"].append(dict(data["repositories"][0]))
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_configuration(Path(directory), data)
            with self.assertRaisesRegex(mirror.MirrorError, "Duplicate repository id"):
                mirror.load_configuration(path)

    def test_rejects_unknown_fields(self):
        data = configuration_data()
        data["repositories"][0]["typo"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_configuration(Path(directory), data)
            with self.assertRaisesRegex(mirror.MirrorError, "unknown field"):
                mirror.load_configuration(path)

    def test_rejects_insecure_upstream_url(self):
        data = configuration_data()
        data["repositories"][0]["url"] = "http://charts.example.test"
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_configuration(Path(directory), data)
            with self.assertRaisesRegex(mirror.MirrorError, "must use HTTPS"):
                mirror.load_configuration(path)


if __name__ == "__main__":
    unittest.main()
