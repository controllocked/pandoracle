from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from pandoracle import device_host
from pandoracle.device_models import (
    HostDeviceConfig,
    HostDevicePolicy,
    load_host_devices,
    save_host_devices,
)


def test_detection_installs_terminal_launcher_without_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(device_host, "_pandoracle_command", lambda: ("/opt/pandoracle",))

    device_host.enable_detection(start=False)

    launcher = (
        tmp_path / "data/applications/org.pandoracle.DeviceOpen.desktop"
    ).read_text(encoding="utf-8")
    autostart = (
        tmp_path / "config/autostart/org.pandoracle.DeviceWatch.desktop"
    ).read_text(encoding="utf-8")
    assert "Terminal=true" in launcher
    assert '"/opt/pandoracle" "device" "open" "--auto" %u' in launcher
    assert "hold" not in launcher.lower()
    assert "Terminal=false" in autostart
    assert '"device" "watch"' in autostart
    assert load_host_devices().detection_enabled

    device_host.disable_detection()
    assert not load_host_devices().detection_enabled
    assert not (tmp_path / "config/autostart/org.pandoracle.DeviceWatch.desktop").exists()
    assert (tmp_path / "data/applications/org.pandoracle.DeviceOpen.desktop").exists()


def test_watcher_opens_once_and_ignores_unrelated_later_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    policy = HostDevicePolicy(
        str(uuid.uuid4()), "Drive", "PUBLIC-UUID", "LUKS-UUID", auto_open=True
    )
    save_host_devices(HostDeviceConfig(True, {policy.device_id: policy}))
    launched: list[str] = []
    monkeypatch.setattr(device_host, "_launch", launched.append)

    class FakeUDisks:
        def connected_for_policy(self, _policy: HostDevicePolicy) -> object:
            return object()

        def monitor_events(self, callback: object, _stopped: object) -> None:
            assert callable(callback)
            callback()
            time.sleep(0.1)
            device_host.disable_detection()
            callback()

    device_host.run_watcher(FakeUDisks())  # type: ignore[arg-type]

    assert launched == [policy.device_id]
