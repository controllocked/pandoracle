from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path

from pandoracle.config import active_device_workspace, runtime_directory
from pandoracle.device_models import load_host_devices, save_host_devices
from pandoracle.device_udisks import UDisksClient
from pandoracle.errors import DeviceError
from pandoracle.fs import write_text_atomic

AUTOSTART_NAME = "org.pandoracle.DeviceWatch.desktop"
LAUNCHER_NAME = "org.pandoracle.DeviceOpen.desktop"


def detection_enabled() -> bool:
    return load_host_devices().detection_enabled


def enable_detection(*, start: bool = True) -> None:
    command = _pandoracle_command()
    _write_text_atomic(
        _applications_directory() / LAUNCHER_NAME,
        _desktop_entry(
            name="Pandoracle device",
            comment="Open a private Pandoracle workspace",
            command=(*command, "device", "open", "--auto", "%u"),
            terminal=True,
            extra="MimeType=x-scheme-handler/pandoracle-device;\nNoDisplay=true\n",
        ),
    )
    config = load_host_devices()
    config.detection_enabled = True
    save_host_devices(config)
    _write_text_atomic(
        _autostart_directory() / AUTOSTART_NAME,
        _desktop_entry(
            name="Pandoracle device detection",
            comment="Recognize trusted Pandoracle removable drives",
            command=(*command, "device", "watch"),
            terminal=False,
            extra="X-GNOME-Autostart-enabled=true\n",
        ),
    )
    if start:
        subprocess.Popen(
            [*command, "device", "watch"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def disable_detection() -> None:
    config = load_host_devices()
    config.detection_enabled = False
    save_host_devices(config)
    try:
        (_autostart_directory() / AUTOSTART_NAME).unlink(missing_ok=True)
    except OSError as error:
        raise DeviceError(f"cannot remove Pandora desktop autostart: {error}") from error


def run_watcher(udisks: UDisksClient | None = None) -> None:
    client = udisks or UDisksClient()
    lock_path = runtime_directory() / "device-watcher.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        stopped = threading.Event()
        changed = threading.Event()
        changed.set()

        def stop(_signum: int, _frame: object) -> None:
            stopped.set()
            changed.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        monitor = threading.Thread(
            target=_monitor_events,
            args=(client, changed, stopped),
            daemon=True,
        )
        monitor.start()
        previously_present: set[str] = set()
        while not stopped.is_set():
            changed.wait(timeout=1)
            changed.clear()
            config = load_host_devices()
            if not config.detection_enabled:
                return
            present: set[str] = set()
            for policy in config.devices.values():
                if client.connected_for_policy(policy) is None:
                    continue
                present.add(policy.device_id)
                if (
                    policy.auto_open
                    and policy.device_id not in previously_present
                    and active_device_workspace() is None
                ):
                    with suppress(DeviceError):
                        _launch(policy.device_id)
            previously_present = present
    finally:
        os.close(descriptor)


def _monitor_events(
    client: UDisksClient, changed: threading.Event, stopped: threading.Event
) -> None:
    try:
        client.monitor_events(changed.set, stopped)
    except DeviceError:
        stopped.set()
        changed.set()


def _launch(device_id: str) -> None:
    gio = shutil.which("gio")
    if gio is None:
        raise DeviceError("automatic opening requires the desktop 'gio' command")
    launcher = _applications_directory() / LAUNCHER_NAME
    if not launcher.exists():
        raise DeviceError("Pandoracle device desktop launcher is not installed")
    subprocess.Popen(
        [gio, "launch", str(launcher), f"pandoracle-device://{device_id}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _pandoracle_command() -> tuple[str, ...]:
    executable = shutil.which("pandoracle")
    if executable:
        return (str(Path(executable).absolute()),)
    return (str(Path(sys.executable).absolute()), "-m", "pandoracle")


def _desktop_entry(
    *,
    name: str,
    comment: str,
    command: tuple[str, ...],
    terminal: bool,
    extra: str,
) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment={comment}\n"
        f"Exec={' '.join(_desktop_quote(item) for item in command)}\n"
        f"Terminal={'true' if terminal else 'false'}\n"
        f"{extra}"
    )


def _desktop_quote(value: str) -> str:
    if value == "%u":
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    escaped = escaped.replace("$", "\\$")
    return f'"{escaped}"'


def _config_home() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value).expanduser() if value else Path.home() / ".config"


def _data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME")
    return Path(value).expanduser() if value else Path.home() / ".local" / "share"


def _autostart_directory() -> Path:
    return _config_home() / "autostart"


def _applications_directory() -> Path:
    return _data_home() / "applications"


def _write_text_atomic(path: Path, value: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, value)
    except OSError as error:
        raise DeviceError(f"cannot install Pandora desktop integration: {error}") from error
