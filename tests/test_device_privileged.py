from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pandoracle import device_privileged
from pandoracle.device_privileged import (
    PrivilegedDeviceProvisioner,
    PrivilegedLuksKeyslotManager,
)
from pandoracle.device_udisks import DriveCandidate, ProvisionedDevice


class NonClosingBytesIO(io.BytesIO):
    def close(self) -> None:
        return None


class FakeProcess:
    def __init__(self, command: list[str], **_kwargs: Any) -> None:
        self.command = command
        self.stdin = NonClosingBytesIO()
        self.stdout = io.BytesIO(
            b'{"status":"ready","drive_path":"/drive","public_path":"/public",'
            b'"encrypted_path":"/encrypted","public_uuid":"PUBLIC",'
            b'"luks_uuid":"LUKS","encrypted_device":"/dev/sdz2"}\n'
            b'{"status":"done"}\n'
        )
        self.stderr = io.BytesIO()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9


def test_privileged_provisioning_uses_one_helper_process_and_pipe_credentials(
    monkeypatch,
) -> None:
    processes: list[FakeProcess] = []

    def factory(command: list[str], **kwargs: Any) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(
        "pandoracle.device_privileged._validate_system_executable", lambda _path: None
    )
    monkeypatch.setattr(
        "pandoracle.device_privileged._validate_root_owned_chain", lambda _path: None
    )
    provisioner = PrivilegedDeviceProvisioner(
        helper_path=Path("/usr/libexec/pandoracle-device-provision"),
        pkexec_path=Path("/usr/bin/pkexec"),
        process_factory=factory,
    )
    candidate = DriveCandidate(
        "/drive", "/block", Path("/dev/sdz"), "Synthetic", "SERIAL", 2**30, "usb"
    )

    transaction = provisioner.start(
        candidate,
        main_passphrase=bytearray(b"main secret"),
        recovery_credential=bytearray(b"recovery secret"),
        host_credential=bytearray(b"host secret"),
    )
    transaction.finish("commit")

    assert len(processes) == 1
    assert processes[0].command == [
        "/usr/bin/pkexec",
        "/usr/libexec/pandoracle-device-provision",
    ]
    request = json.loads(processes[0].stdin.getvalue().splitlines()[0])
    assert request["action"] == "provision"
    assert b"main secret" not in b"\0".join(item.encode() for item in processes[0].command)
    assert request["main"] != "main secret"
    assert processes[0].stdin.getvalue().endswith(b"commit\n")


def test_host_key_enrollment_uses_one_privileged_helper_invocation(monkeypatch) -> None:
    calls: list[tuple[list[str], bytes]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, b'{"status":"done"}\n', b"")

    monkeypatch.setattr(
        "pandoracle.device_privileged._validate_system_executable", lambda _path: None
    )
    monkeypatch.setattr(
        "pandoracle.device_privileged._validate_root_owned_chain", lambda _path: None
    )
    manager = PrivilegedLuksKeyslotManager(
        helper_path=Path("/usr/libexec/pandoracle-device-provision"),
        pkexec_path=Path("/usr/bin/pkexec"),
        runner=runner,
    )

    manager.add_key(
        Path("/dev/sdz2"),
        existing_credential=bytearray(b"owner secret"),
        new_credential=bytearray(b"host secret"),
        keyslot=2,
    )

    assert len(calls) == 1
    assert calls[0][0] == [
        "/usr/bin/pkexec",
        "/usr/libexec/pandoracle-device-provision",
    ]
    assert all("secret" not in item for item in calls[0][0])
    request = json.loads(calls[0][1])
    assert request["action"] == "add-key"
    assert request["keyslot"] == 2


def test_privileged_helper_enrolls_recovery_and_host_slots(
    tmp_path: Path, monkeypatch
) -> None:
    candidate = DriveCandidate(
        "/drive", "/block", Path("/dev/sdz"), "Synthetic", "SERIAL", 2**30, "usb"
    )
    provisioned = ProvisionedDevice(
        "/drive",
        "/public",
        "/encrypted",
        "/cleartext",
        "PUBLIC",
        "LUKS",
        Path("/dev/sdz2"),
    )

    class FakeUDisks:
        def provision(self, selected: DriveCandidate, _credential: bytearray):
            assert selected == candidate
            return provisioned

        def mount(self, _path: str) -> Path:
            return tmp_path

        def unmount(self, _path: str) -> None:
            return None

        def lock(self, _path: str) -> None:
            return None

        def wait_until_locked(self, _path: str) -> None:
            return None

    class FakeKeyslots:
        def __init__(self) -> None:
            self.slots = {0}
            self.added: list[int] = []

        def occupied_slots(self, _device: Path) -> set[int]:
            return set(self.slots)

        def add_key(
            self,
            _device: Path,
            *,
            existing_credential: bytearray,
            new_credential: bytearray,
            keyslot: int,
        ) -> None:
            assert existing_credential
            assert new_credential
            self.slots.add(keyslot)
            self.added.append(keyslot)

        def remove_key(self, _device: Path, _credential: bytearray, *, keyslot: int) -> None:
            self.slots.remove(keyslot)

    keyslots = FakeKeyslots()
    request = {
        "version": 1,
        "action": "provision",
        "main": device_privileged._encode_secret(b"main"),
        "recovery": device_privileged._encode_secret(b"recovery"),
        "host": device_privileged._encode_secret(b"host"),
    }
    replies: list[dict[str, Any]] = []
    monkeypatch.setattr(device_privileged, "_validate_privileged_process", os.getuid)
    monkeypatch.setattr(device_privileged, "_read_request", lambda _stream: request)
    monkeypatch.setattr(
        device_privileged, "_validated_candidate", lambda _request, _uid: candidate
    )
    monkeypatch.setattr(device_privileged, "UDisksClient", FakeUDisks)
    monkeypatch.setattr(
        device_privileged, "LuksKeyslotManager", lambda *, elevate: keyslots
    )
    monkeypatch.setattr(device_privileged, "sync_filesystem", lambda _path: None)
    monkeypatch.setattr(device_privileged.os, "chown", lambda *_args: None)
    monkeypatch.setattr(device_privileged, "_write_reply", replies.append)
    monkeypatch.setattr(
        device_privileged.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(b"commit\n")),
    )

    device_privileged.privileged_helper_main()

    assert keyslots.added == [1, 2]
    assert replies[0]["status"] == "ready"
    assert replies[-1] == {"status": "done"}
