import json
import tempfile
import unittest
from pathlib import Path

from scripts import mirror

from tests.test_config import configuration_data


class FakeRunner:
    def __init__(self, fail_on_push: int | None = None):
        self.calls: list[list[str]] = []
        self.metadata: dict[str, tuple[str, str]] = {}
        self.pushes = 0
        self.fail_on_push = fail_on_push

    def run(self, arguments: list[str], *, env=None) -> str:
        self.calls.append(arguments)
        if arguments[:3] == ["helm", "repo", "add"]:
            return ""
        if arguments[:3] == ["helm", "repo", "update"]:
            return ""
        if arguments[:2] == ["helm", "pull"]:
            chart = arguments[2].split("/", 1)[1]
            version = arguments[arguments.index("--version") + 1]
            destination = Path(arguments[arguments.index("--destination") + 1])
            archive = destination / f"{chart}-{version}.tgz"
            archive.write_bytes(f"{chart}@{version}".encode())
            self.metadata[str(archive)] = (chart, version)
            return ""
        if arguments[:3] == ["helm", "show", "chart"]:
            chart, version = self.metadata[arguments[3]]
            return f"apiVersion: v2\nname: {chart}\nversion: {version}\n"
        if arguments[:2] == ["helm", "push"]:
            self.pushes += 1
            if self.fail_on_push == self.pushes:
                raise mirror.MirrorError("simulated push failure")
            return "Pushed"
        raise AssertionError(f"Unexpected command: {arguments}")


def write_repository(root: Path):
    config_path = root / "config/repositories.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(configuration_data()), encoding="utf-8")
    state_path = root / "state/example.json"
    mirror.save_state(state_path, mirror.empty_state())
    config = mirror.load_configuration(config_path)
    plan = {
        "schema": 1,
        "created_at": "2026-07-30T00:00:00+00:00",
        "repositories": [
            {
                "id": "example",
                "url": "https://charts.example.test",
                "oci_repository": "oci://ghcr.io/example/helm-chart/example",
                "state": "state/example.json",
                "mode": "all",
                "mark_initialized": True,
                "skip_after_success": [],
                "releases": [
                    {"chart": "main", "version": "2.0.0", "app_version": "2"},
                    {"chart": "main", "version": "1.0.0", "app_version": "1"},
                ],
            }
        ],
    }
    return config, plan, state_path


class PublishTests(unittest.TestCase):
    def test_publish_validates_metadata_and_checkpoints_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            config, plan, state_path = write_repository(Path(directory))
            runner = FakeRunner()
            count = mirror.publish_plan(
                plan, config, runner=runner, dry_run=False
            )
            state = mirror.load_state(state_path)
            self.assertEqual(count, 2)
            self.assertTrue(state["initialized"])
            self.assertEqual(set(state["published"]), {"main@1.0.0", "main@2.0.0"})
            self.assertTrue(
                all(
                    digest.startswith("sha256:")
                    for digest in state["published"].values()
                )
            )
            self.assertEqual(runner.pushes, 2)

    def test_partial_failure_is_checkpointed_and_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            config, plan, state_path = write_repository(Path(directory))
            failing_runner = FakeRunner(fail_on_push=2)
            with self.assertRaisesRegex(mirror.MirrorError, "simulated"):
                mirror.publish_plan(
                    plan, config, runner=failing_runner, dry_run=False
                )
            partial_state = mirror.load_state(state_path)
            self.assertEqual(set(partial_state["published"]), {"main@2.0.0"})
            self.assertFalse(partial_state["initialized"])

            retry_runner = FakeRunner()
            count = mirror.publish_plan(
                plan, config, runner=retry_runner, dry_run=False
            )
            final_state = mirror.load_state(state_path)
            self.assertEqual(count, 1)
            self.assertEqual(retry_runner.pushes, 1)
            self.assertTrue(final_state["initialized"])
            self.assertEqual(
                set(final_state["published"]), {"main@1.0.0", "main@2.0.0"}
            )

    def test_dry_run_does_not_change_state_or_invoke_helm(self):
        with tempfile.TemporaryDirectory() as directory:
            config, plan, state_path = write_repository(Path(directory))
            before = state_path.read_text(encoding="utf-8")
            runner = FakeRunner()
            count = mirror.publish_plan(plan, config, runner=runner, dry_run=True)
            self.assertEqual(count, 2)
            self.assertEqual(runner.calls, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), before)

    def test_empty_final_batch_can_mark_state_initialized_without_helm(self):
        with tempfile.TemporaryDirectory() as directory:
            config, plan, state_path = write_repository(Path(directory))
            plan["repositories"][0]["releases"] = []
            runner = FakeRunner()
            count = mirror.publish_plan(
                plan, config, runner=runner, dry_run=False
            )
            self.assertEqual(count, 0)
            self.assertEqual(runner.calls, [])
            self.assertTrue(mirror.load_state(state_path)["initialized"])

    def test_plan_rejects_changed_oci_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            config, plan, _ = write_repository(Path(directory))
            plan["repositories"][0]["oci_repository"] = (
                "oci://ghcr.io/attacker/other"
            )
            with self.assertRaisesRegex(mirror.MirrorError, "differs"):
                mirror.publish_plan(
                    plan, config, runner=FakeRunner(), dry_run=True
                )


if __name__ == "__main__":
    unittest.main()
