#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Prepare and boot the release-test simulator; never build, sign, or deploy."""

import json
import os
import shlex
import subprocess
import uuid


def run(*args, timeout=120):
    print(f"+ {shlex.join(args)}", flush=True)
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout)
    print(result.stdout, end="", flush=True)
    result.check_returncode()
    return result.stdout.strip()


def inventory():
    return json.loads(run("xcrun", "simctl", "list", "--json"))


def runtime_for(data, sdk):
    def version(value):
        return tuple(int(part) for part in value.split(".")[:2])

    return next((runtime for runtime in data["runtimes"]
                 if runtime["identifier"].startswith("com.apple.CoreSimulator.SimRuntime.iOS-")
                 and runtime.get("isAvailable", False)
                 and version(runtime["version"]) == version(sdk)), None)


def prepare():
    device_name = os.environ.get("TEST_DEVICE", "iPhone 17")
    run("sw_vers")
    run("uname", "-m")
    developer = run("xcode-select", "-p")
    actual = run("xcodebuild", "-version").splitlines()[0].removeprefix("Xcode ")
    expected = os.environ["XCODE_VERSION"]
    # Runner aliases may resolve 26.1 to the compatible 26.1.1 patch release.
    if actual != expected and not actual.startswith(expected + "."):
        raise RuntimeError(f"Selected Xcode {actual}; expected {expected}")

    # Apple requires this after selecting Xcode and before platform installation.
    run("sudo", "xcodebuild", "-runFirstLaunch", timeout=300)
    sdk = run("xcrun", "--sdk", "iphonesimulator", "--show-sdk-version")
    try:
        data = inventory()  # Also starts the user's CoreSimulator service.
    except (subprocess.SubprocessError, json.JSONDecodeError):
        # A service from the runner's default Xcode may still be active. Recover
        # once, as the runner user, then require a working simctl connection.
        subprocess.run(["killall", "-u", run("id", "-un"),
                        "com.apple.CoreSimulator.CoreSimulatorService"], check=False)
        run("open", "-a", developer + "/Applications/Simulator.app")
        data = inventory()

    runtime = runtime_for(data, sdk)
    if runtime is None:
        # Do not download the newest OS for an older pinned Xcode. A runner SDK
        # being installed does not imply that its matching runtime is installed.
        run("xcodebuild", "-downloadPlatform", "iOS", "-buildVersion", sdk, timeout=900)
        data = inventory()
        runtime = runtime_for(data, sdk)
    if runtime is None:
        raise RuntimeError(f"No available iOS {sdk} runtime after installation")

    devices = data["devices"].get(runtime["identifier"], [])
    device = next((item for item in devices
                   if item["name"] == device_name and item.get("isAvailable", False)), None)
    if device is None:
        device_type = next((item for item in data["devicetypes"]
                            if item["name"] == device_name), None)
        if device_type is None:
            raise RuntimeError(f"Unknown simulator device type: {device_name}")
        udid = run("xcrun", "simctl", "create", device_name,
                   device_type["identifier"], runtime["identifier"])
    else:
        udid = device["udid"]
    udid = str(uuid.UUID(udid))  # Validate before using as a destination/env value.
    if device is None or device["state"] != "Booted":
        run("xcrun", "simctl", "boot", udid)
    run("xcrun", "simctl", "bootstatus", udid, "-b", timeout=300)
    ready = inventory()["devices"].get(runtime["identifier"], [])
    if not any(item["udid"].lower() == udid and item["state"] == "Booted"
               and item.get("isAvailable", False) for item in ready):
        raise RuntimeError("Simulator did not reach Booted state")

    # Pin Fastlane to the exact simulator whose readiness was verified.
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as output:
        output.write(f"TEST_SIMULATOR_UDID={udid}\n")
    print(f"Simulator ready: {device_name}, iOS {runtime['version']}, {udid}")


if __name__ == "__main__":
    prepare()
