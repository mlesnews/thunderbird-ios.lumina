# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("setup", ROOT / "CI/setup_ios_simulator.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
UDID = "12345678-1234-1234-1234-123456789abc"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-26-1"


def inventory(version="26.1", available=True, state="Shutdown", devices=True):
    return {
        "runtimes": [{"identifier": RUNTIME, "version": version,
                      "isAvailable": available}],
        "devices": {RUNTIME: [{"name": "iPhone 17", "udid": UDID,
                               "isAvailable": True, "state": state}] if devices else []},
        "devicetypes": [{"name": "iPhone 17", "identifier": "iPhone-17"}],
    }


class SimulatorSetupTests(unittest.TestCase):
    def test_workflow_propagates_setup_failure_and_keeps_release_gates(self):
        workflow = (ROOT / ".github/workflows/shippable-builds.yml").read_text()
        # Explicit bash supplies pipefail so tee cannot turn a failed setup green.
        self.assertEqual(workflow.count("name: Prepare and boot iOS test simulator\n        shell: bash"), 2)
        preflight, release = workflow.split("  simulator_preflight:\n", 1)[1].split("  build_and_deploy_ios:\n", 1)
        self.assertNotIn("secrets.", preflight)
        self.assertNotIn("environment:", preflight)
        self.assertNotIn("fastlane beta", preflight)
        self.assertIn("github.event_name == 'push' || github.event_name == 'pull_request'", preflight)
        self.assertIn("environment: thunderbird_release", release)
        self.assertIn("github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'", release)
        beta = (ROOT / "fastlane/Fastfile").read_text().split("lane :beta do", 1)[1].split("lane :release do", 1)[0]
        self.assertLess(beta.index("run_tests("), beta.index("build_app("))
        self.assertLess(beta.index("build_app("), beta.index("upload_to_testflight("))
        self.assertIn('id=#{ENV[\'TEST_SIMULATOR_UDID\']}', beta)

    def test_command_failure_and_timeout_propagate(self):
        with self.assertRaises(subprocess.CalledProcessError):
            setup.run("python3", "-c", "raise SystemExit(70)")
        with patch.object(setup.subprocess, "run", side_effect=subprocess.TimeoutExpired("simctl", 120)):
            with self.assertRaises(subprocess.TimeoutExpired):
                setup.run("xcrun", "simctl", "list")

    def exercise(self, inventories, *, actual="26.1.1", fail=None):
        self.calls = []
        snapshots = iter(inventories)

        def run(*args, **kwargs):
            self.calls.append(args)
            if fail and fail in args:
                raise subprocess.CalledProcessError(70, args)
            if args == ("xcode-select", "-p"):
                return "/Applications/Xcode_26.1.app/Contents/Developer"
            if args == ("xcodebuild", "-version"):
                return f"Xcode {actual}\nBuild version 17B100"
            if "--show-sdk-version" in args:
                return "26.1"
            if args == ("xcrun", "simctl", "list", "--json"):
                result = next(snapshots)
                if isinstance(result, Exception):
                    raise result
                return json.dumps(result)
            if "create" in args:
                return UDID
            return "runner"

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "env"
            with patch.dict(os.environ, XCODE_VERSION="26.1", TEST_DEVICE="iPhone 17",
                            GITHUB_ENV=str(output)), patch.object(setup, "run", run):
                try:
                    setup.prepare()
                finally:
                    self.env = output.read_text() if output.exists() else ""

    def test_reuses_installed_runtime_and_exports_only_after_boot(self):
        self.exercise([inventory(), inventory(state="Booted")])
        self.assertFalse(any("-downloadPlatform" in c for c in self.calls))
        self.assertEqual(self.env, f"TEST_SIMULATOR_UDID={UDID}\n")
        self.assertLess(self.calls.index(("sudo", "xcodebuild", "-runFirstLaunch")),
                        self.calls.index(("xcrun", "simctl", "list", "--json")))
        self.assertIn(("xcrun", "simctl", "bootstatus", UDID, "-b"), self.calls)

    def test_newer_or_unavailable_runtime_requires_sdk_matched_download(self):
        for initial in [inventory(version="26.5"), inventory(available=False)]:
            with self.subTest(initial=initial):
                self.exercise([initial, inventory(), inventory(state="Booted")])
                self.assertIn(("xcodebuild", "-downloadPlatform", "iOS", "-buildVersion", "26.1"), self.calls)

    def test_creates_missing_device_on_matching_runtime(self):
        self.exercise([inventory(devices=False), inventory(state="Booted")])
        self.assertIn(("xcrun", "simctl", "create", "iPhone 17", "iPhone-17", RUNTIME), self.calls)

    def test_already_booted_device_still_requires_bootstatus(self):
        self.exercise([inventory(state="Booted"), inventory(state="Booted")])
        self.assertFalse(any("boot" in c for c in self.calls))
        self.assertTrue(any("bootstatus" in c for c in self.calls))

    def test_recovers_service_connection_once(self):
        failure = subprocess.CalledProcessError(70, "simctl")
        with patch.object(setup.subprocess, "run") as recovery:
            self.exercise([failure, inventory(), inventory(state="Booted")])
        recovery.assert_called_once()
        self.assertIn("CoreSimulatorService", recovery.call_args.args[0][-1])
        self.assertTrue(any(c[0] == "open" for c in self.calls))

    def test_persistent_service_failure_is_fatal(self):
        failure = subprocess.CalledProcessError(70, "simctl")
        with patch.object(setup.subprocess, "run"), self.assertRaises(subprocess.CalledProcessError):
            self.exercise([failure, failure])
        self.assertEqual(self.env, "")

    def test_download_and_boot_failures_do_not_export_readiness(self):
        for fail, snapshots in [("-downloadPlatform", [inventory(available=False)]),
                                ("bootstatus", [inventory()])]:
            with self.subTest(fail=fail), self.assertRaises(subprocess.CalledProcessError):
                self.exercise(snapshots, fail=fail)
            self.assertEqual(self.env, "")

    def test_missing_runtime_after_download_is_fatal(self):
        with self.assertRaisesRegex(RuntimeError, "No available iOS"):
            self.exercise([inventory(available=False), inventory(available=False)])
        self.assertEqual(self.env, "")

    def test_incorrect_xcode_is_fatal(self):
        with self.assertRaisesRegex(RuntimeError, "expected 26.1"):
            self.exercise([], actual="26.6")
        self.assertEqual(self.env, "")

    def test_unknown_device_and_unbooted_device_are_fatal(self):
        unknown = inventory(devices=False)
        unknown["devicetypes"] = []
        for snapshots in [[unknown], [inventory(), inventory()]]:
            with self.subTest(snapshots=snapshots), self.assertRaises(RuntimeError):
                self.exercise(snapshots)
            self.assertEqual(self.env, "")

    def test_invalid_udid_is_not_exported(self):
        invalid = copy.deepcopy(inventory())
        invalid["devices"][RUNTIME][0]["udid"] = "bad\nINJECTED=value"
        with self.assertRaises(ValueError):
            self.exercise([invalid])
        self.assertEqual(self.env, "")


if __name__ == "__main__":
    unittest.main()
