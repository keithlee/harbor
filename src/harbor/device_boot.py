"""Boot the devices a ``[builder] device_proof`` builder screenshots, before it launches.

The builder's Bash sandbox can reach a running emulator or simulator but must
not start one: an emulator registers Mach services (``bootstrap_check_in``)
and a simulator boot needs CoreSimulator writes no sandbox rule should grant.
Harbor runs outside the sandbox, so it makes sure one Android emulator and one
iOS simulator are up first and tells the builder what happened, quoting the
literal error when a boot fails.

Selection is deterministic and never guessed per machine: an attached or
booted device is used as is; otherwise the first AVD by name, and the first
available iPhone on the newest iOS runtime.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

Run = Callable[..., subprocess.CompletedProcess]
ANDROID_BOOT_SECONDS = 240
IOS_BOOT_SECONDS = 300


@dataclass
class DeviceStatus:
    platform: str
    ready: bool
    detail: str

    def line(self) -> str:
        state = "ready" if self.ready else "NOT ready"
        return f"- {self.platform}: {state}: {self.detail}"


def _sdk_tool(name: str, subdir: str, env: dict[str, str]) -> str | None:
    found = shutil.which(name, path=env.get("PATH"))
    if found:
        return found
    roots = [env.get("ANDROID_HOME"), env.get("ANDROID_SDK_ROOT"), str(Path(env.get("HOME", "~")).expanduser() / "Library/Android/sdk")]
    for root in filter(None, roots):
        candidate = Path(root) / subdir / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _output(result: subprocess.CompletedProcess) -> str:
    text = "\n".join(part for part in (result.stdout or "", result.stderr or "") if part).strip()
    return text.splitlines()[-1] if text else f"exit {result.returncode}"


def _call(run: Run, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return run(list(argv), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", f"timed out after {timeout:.0f}s: {' '.join(argv)}")
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", f"{argv[0]}: {exc}")


def attached_android(adb: str, run: Run) -> list[str]:
    result = _call(run, [adb, "devices"], 30)
    return [line.split()[0] for line in (result.stdout or "").splitlines()[1:] if re.search(r"\tdevice\s*$", line)]


def prepare_android(env: dict[str, str], run: Run = subprocess.run,
                    spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
                    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                    boot_seconds: float = ANDROID_BOOT_SECONDS) -> DeviceStatus:
    adb = _sdk_tool("adb", "platform-tools", env)
    if adb is None:
        return DeviceStatus("android", False, "adb not found on PATH, in $ANDROID_HOME or in ~/Library/Android/sdk; install the Android SDK platform-tools")
    started = _call(run, [adb, "start-server"], 60)
    if started.returncode != 0:
        return DeviceStatus("android", False, f"`adb start-server` failed: {_output(started)}")
    devices = attached_android(adb, run)
    if devices:
        return DeviceStatus("android", True, f"{devices[0]} already attached (adb server on 127.0.0.1:5037)")
    emulator = _sdk_tool("emulator", "emulator", env)
    if emulator is None:
        return DeviceStatus("android", False, "no device attached and the Android emulator is not installed")
    listed = _call(run, [emulator, "-list-avds"], 60)
    avds = sorted(line.strip() for line in (listed.stdout or "").splitlines() if line.strip() and not line.startswith(("INFO", "WARNING", "ERROR")))
    if listed.returncode != 0 or not avds:
        return DeviceStatus("android", False, f"no AVD to boot ({_output(listed)}); create one in Android Studio's Device Manager")
    log = tempfile.NamedTemporaryFile("w+", prefix="harbor-emulator-", suffix=".log", delete=False)
    try:
        process = spawn([emulator, "-avd", avds[0], "-no-snapshot-save"], stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        return DeviceStatus("android", False, f"could not start `emulator -avd {avds[0]}`: {exc}")
    deadline = clock() + boot_seconds
    while clock() < deadline:
        if process.poll() is not None:
            log.seek(0)
            tail = [line for line in log.read().splitlines() if line.strip()][-3:]
            return DeviceStatus("android", False, f"`emulator -avd {avds[0]}` exited {process.returncode}: {' | '.join(tail) or 'no output'}")
        booted = _call(run, [adb, "shell", "getprop", "sys.boot_completed"], 15)
        if booted.returncode == 0 and (booted.stdout or "").strip() == "1":
            serial = (attached_android(adb, run) or ["emulator"])[0]
            return DeviceStatus("android", True, f"booted AVD {avds[0]} as {serial} (log {log.name})")
        sleep(3)
    return DeviceStatus("android", False, f"AVD {avds[0]} did not finish booting within {boot_seconds:.0f}s (log {log.name})")


def _runtime_version(key: str) -> tuple[int, ...]:
    match = re.search(r"iOS-([0-9-]+)$", key)
    return tuple(int(part) for part in match.group(1).split("-")) if match else ()


def prepare_ios(run: Run = subprocess.run, boot_seconds: float = IOS_BOOT_SECONDS) -> DeviceStatus:
    if shutil.which("xcrun") is None:
        return DeviceStatus("ios", False, "xcrun not found; install Xcode")
    listed = _call(run, ["xcrun", "simctl", "list", "devices", "-j"], 60)
    try:
        runtimes = json.loads(listed.stdout or "")["devices"]
    except (ValueError, KeyError, TypeError):
        return DeviceStatus("ios", False, f"`xcrun simctl list devices -j` failed: {_output(listed)}")
    booted = [(d["name"], d["udid"]) for devices in runtimes.values() for d in devices if d.get("state") == "Booted"]
    if booted:
        return DeviceStatus("ios", True, f"{booted[0][0]} ({booted[0][1]}) already booted")
    ios = sorted((key for key in runtimes if _runtime_version(key)), key=_runtime_version, reverse=True)
    phones = [(d["name"], d["udid"]) for key in ios[:1] for d in sorted(runtimes[key], key=lambda d: d.get("name", ""))
              if d.get("isAvailable") and str(d.get("name", "")).startswith("iPhone")]
    if not phones:
        return DeviceStatus("ios", False, "no available iPhone simulator; add one in Xcode's Devices and Simulators window")
    name, udid = phones[0]
    boot = _call(run, ["xcrun", "simctl", "boot", udid], 120)
    if boot.returncode != 0:
        return DeviceStatus("ios", False, f"`xcrun simctl boot {udid}` ({name}) failed: {_output(boot)}")
    status = _call(run, ["xcrun", "simctl", "bootstatus", udid, "-b"], boot_seconds)
    if status.returncode != 0:
        return DeviceStatus("ios", False, f"`xcrun simctl bootstatus {udid} -b` ({name}) failed: {_output(status)}")
    return DeviceStatus("ios", True, f"booted {name} ({udid})")


def prepare_devices(platforms: Sequence[str], env: dict[str, str] | None = None, run: Run = subprocess.run) -> list[DeviceStatus]:
    """Make sure each enabled mobile platform has one running device. ``web`` needs none."""

    env = dict(os.environ if env is None else env)
    statuses = []
    if "android" in platforms:
        statuses.append(prepare_android(env, run))
    if "ios" in platforms:
        statuses.append(prepare_ios(run))
    return statuses


def device_note(statuses: Sequence[DeviceStatus]) -> str:
    """The text Harbor appends to the builder's prompt; empty when nothing was prepared."""

    if not statuses:
        return ""
    lines = ["Harbor device proof: before this turn Harbor checked, and booted where needed, one device per "
             "platform outside your sandbox. Do not boot, shut down or erase devices yourself."]
    lines += [status.line() for status in statuses]
    if any(not status.ready for status in statuses):
        lines.append("A device that is NOT ready cannot be used this turn. Quote the error above when you report the "
                     "missing proof as a blocker; do not work around the sandbox.")
    return "\n".join(lines)
