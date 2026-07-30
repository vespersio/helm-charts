import subprocess
import unittest
from unittest import mock

from scripts import mirror


class CommandRunnerTests(unittest.TestCase):
    @mock.patch("scripts.mirror.time.sleep")
    @mock.patch("scripts.mirror.subprocess.run")
    def test_retries_transient_command_failure_with_backoff(
        self, run_mock, sleep_mock
    ):
        run_mock.side_effect = [
            subprocess.CalledProcessError(1, ["helm", "push"]),
            subprocess.CompletedProcess(["helm", "push"], 0, stdout="Pushed\n"),
        ]

        output = mirror.CommandRunner().run(
            ["helm", "push", "chart.tgz", "oci://registry/repository"],
            attempts=4,
        )

        self.assertEqual(output, "Pushed\n")
        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(2)

    @mock.patch("scripts.mirror.time.sleep")
    @mock.patch("scripts.mirror.subprocess.run")
    def test_reports_failure_after_all_attempts(self, run_mock, sleep_mock):
        run_mock.side_effect = subprocess.CalledProcessError(
            1, ["helm", "push"]
        )

        with self.assertRaisesRegex(
            mirror.MirrorError, "failed after 3 attempt"
        ):
            mirror.CommandRunner().run(
                ["helm", "push", "chart.tgz", "oci://registry/repository"],
                attempts=3,
            )

        self.assertEqual(run_mock.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [2, 4],
        )


if __name__ == "__main__":
    unittest.main()
