from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from pandoracle.device_models import (
    HostCredential,
    HostDeviceConfig,
    HostDevicePolicy,
    PrivateDeviceManifest,
    PublicDeviceManifest,
    UnlockMode,
    load_host_devices,
    read_private_manifest,
    read_public_manifest,
    save_host_devices,
    write_private_manifest,
    write_public_manifest,
)
from pandoracle.device_service import (
    DeviceService,
    _acquire_device_session_lock,
    _mount_holders,
)
from pandoracle.device_udisks import ConnectedDevice, DriveCandidate, ProvisionedDevice
from pandoracle.errors import DeviceBusyError, DeviceError
from pandoracle.workspace import Workspace


class FakeKeyslots:
    def __init__(self) -> None:
        self.slots = {0}
        self.added: list[tuple[bytes, bytes, int]] = []
        self.removed: list[bytes] = []

    def occupied_slots(self, _device: Path) -> set[int]:
        return set(self.slots)

    def add_key(
        self,
        _device: Path,
        *,
        existing_credential: bytes | bytearray,
        new_credential: bytes | bytearray,
        keyslot: int,
    ) -> None:
        self.added.append((bytes(existing_credential), bytes(new_credential), keyslot))
        self.slots.add(keyslot)

    def remove_key(
        self, _device: Path, credential: bytes | bytearray, *, keyslot: int
    ) -> None:
        value = bytes(credential)
        self.removed.append(value)
        match = next(item for item in self.added if item[1] == value)
        assert match[2] == keyslot
        self.slots.remove(keyslot)


class FakeSecrets:
    def __init__(self, *, fail_store: bool = False) -> None:
        self.values: dict[tuple[str, str, str], bytes] = {}
        self.fail_store = fail_store

    def store(
        self,
        *,
        device_id: str,
        luks_uuid: str,
        credential_id: str,
        secret: bytes,
    ) -> None:
        if self.fail_store:
            raise DeviceError("mock Secret Service failure")
        self.values[(device_id, luks_uuid, credential_id)] = secret

    def lookup(self, *, device_id: str, luks_uuid: str, credential_id: str) -> bytes | None:
        return self.values.get((device_id, luks_uuid, credential_id))

    def delete(self, *, device_id: str, luks_uuid: str, credential_id: str) -> bool:
        return self.values.pop((device_id, luks_uuid, credential_id), None) is not None


class FakeUDisks:
    def __init__(self, root: Path) -> None:
        self.public_root = root / "public"
        self.private_root = root / "private"
        self.public_root.mkdir()
        self.private_root.mkdir()
        self.closed: list[str] = []
        self.connected: ConnectedDevice | None = None
        self.removed_on_monitor = False
        self.remove_after_busy = False
        self.busy_seen = threading.Event()
        self.unlock_values: list[bytes] = []
        self.rejected_unlock: bytes | None = None
        self.unmount_failures: dict[str, int] = {}

    def provision(
        self, candidate: DriveCandidate, _passphrase: bytes | bytearray
    ) -> ProvisionedDevice:
        value = ProvisionedDevice(
            candidate.object_path,
            "/public",
            "/encrypted",
            "/clear",
            "PUBLIC-UUID",
            "LUKS-UUID",
            Path("/dev/fake-private"),
        )
        self.connected = ConnectedDevice(
            candidate.object_path,
            "/public",
            Path("/dev/fake-public"),
            "/encrypted",
            Path("/dev/fake-private"),
            "PUBLIC-UUID",
            "LUKS-UUID",
            "/clear",
        )
        return value

    def mount(self, path: str) -> Path:
        return self.public_root if path == "/public" else self.private_root

    def mount_points(self, path: str) -> list[Path]:
        if path == "/public":
            return [self.public_root]
        if path == "/clear":
            return [self.private_root]
        return []

    def unmount(self, path: str) -> None:
        if self.unmount_failures.get(path, 0):
            self.unmount_failures[path] -= 1
            self.busy_seen.set()
            raise DeviceBusyError("filesystem is busy")
        self.closed.append(f"unmount:{path}")

    def lock(self, path: str) -> None:
        self.closed.append(f"lock:{path}")

    def power_off(self, path: str) -> None:
        self.closed.append(f"power:{path}")

    def cleartext_path(self, _path: str) -> str | None:
        return "/clear"

    def connected_for_policy(self, _policy: HostDevicePolicy) -> ConnectedDevice | None:
        return self.connected

    def is_present(self, _paths: set[str]) -> bool:
        return self.connected is not None

    def unlock(self, _path: str, _credential: bytes | bytearray) -> str:
        value = bytes(_credential)
        self.unlock_values.append(value)
        if value == self.rejected_unlock:
            raise DeviceError("bad automatic key")
        return "/clear"

    def take_ownership(self, _path: str) -> None:
        return None

    def monitor_removal(
        self,
        _paths: set[str],
        _drive_path: str,
        _closing: threading.Event,
        removed: threading.Event,
        _stopped: threading.Event,
    ) -> None:
        if self.removed_on_monitor:
            time.sleep(0.05)
            removed.set()
        elif self.remove_after_busy:
            self.busy_seen.wait(timeout=2)
            removed.set()


class FakePrivilegedTransaction:
    def __init__(self, provisioned: ProvisionedDevice) -> None:
        self.provisioned = provisioned
        self.outcomes: list[str] = []

    def finish(self, outcome: str) -> None:
        self.outcomes.append(outcome)

    def abort(self) -> None:
        self.outcomes.append("abort")


class FakePrivilegedProvisioner:
    def __init__(self) -> None:
        self.transaction: FakePrivilegedTransaction | None = None
        self.host_credentials: list[bytes | None] = []

    def start(
        self,
        candidate: DriveCandidate,
        *,
        main_passphrase: bytes | bytearray,
        recovery_credential: bytes | bytearray,
        host_credential: bytes | bytearray | None,
    ) -> FakePrivilegedTransaction:
        assert bytes(main_passphrase) == b"main"
        assert recovery_credential
        self.host_credentials.append(
            None if host_credential is None else bytes(host_credential)
        )
        self.transaction = FakePrivilegedTransaction(
            ProvisionedDevice(
                candidate.object_path,
                "/public",
                "/encrypted",
                "",
                "PUBLIC-UUID",
                "LUKS-UUID",
                Path("/dev/fake-private"),
            )
        )
        return self.transaction


def candidate() -> DriveCandidate:
    return DriveCandidate(
        "/drive", "/block", Path("/dev/fake"), "Fake USB", "SERIAL", 2**30, "usb"
    )


@pytest.mark.parametrize("automatic, expected_slots", [(False, {0, 1}), (True, {0, 1, 2})])
def test_setup_uses_fixed_roles_and_persists_no_owner_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    automatic: bool,
    expected_slots: set[int],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    udisks = FakeUDisks(tmp_path)
    keys = FakeKeyslots()
    secrets = FakeSecrets()
    shown: list[str] = []
    main = bytearray(b"owner passphrase")
    public_source = tmp_path / "public-source"
    (public_source / "documents").mkdir(parents=True)
    (public_source / "README.txt").write_text("Synthetic public note\n", encoding="utf-8")
    (public_source / "documents" / "recovery.pdf").write_bytes(b"synthetic-pdf")
    service = DeviceService(udisks=udisks, keyslots=keys, secrets_store=secrets)

    result = service.setup(
        candidate(),
        name="Field drive",
        main_passphrase=main,
        acknowledge_recovery=lambda value: shown.append(value) is None,
        automatic_unlock=automatic,
        public_directory=public_source,
    )

    assert keys.slots == expected_slots
    assert len(shown) == 1
    assert keys.added[0][0] == b"owner passphrase"
    assert keys.added[0][1] == shown[0].encode("ascii")
    assert main == bytearray(len(main))
    public_text = (udisks.public_root / ".pandoracle-device/device.json").read_text()
    private_text = (udisks.private_root / ".pandoracle-device.json").read_text()
    host_text = (tmp_path / "config/pandoracle/devices.json").read_text()
    for text in (public_text, private_text, host_text):
        assert "owner passphrase" not in text
        assert shown[0] not in text
    assert result.workspace_id not in public_text
    assert "LUKS-UUID" not in public_text
    assert "credential" not in public_text.lower()
    assert (udisks.public_root / "README.txt").read_text(encoding="utf-8") == (
        "Synthetic public note\n"
    )
    assert (udisks.public_root / "documents" / "recovery.pdf").read_bytes() == b"synthetic-pdf"
    assert read_public_manifest(udisks.public_root).device_id == result.device_id
    assert len(read_private_manifest(udisks.private_root).host_credentials) == int(automatic)
    assert list(secrets.values.values()) == ([keys.added[-1][1]] if automatic else [])


def test_recovery_cancellation_removes_undisclosed_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    keys = FakeKeyslots()
    service = DeviceService(
        udisks=FakeUDisks(tmp_path), keyslots=keys, secrets_store=FakeSecrets()
    )

    with pytest.raises(DeviceError, match="recovery credential confirmation"):
        service.setup(
            candidate(),
            name="Cancelled",
            main_passphrase=bytearray(b"main"),
            acknowledge_recovery=lambda _value: False,
            automatic_unlock=True,
        )

    assert keys.slots == {0}
    assert len(keys.removed) == 1


def test_privileged_setup_is_one_transaction_and_records_its_host_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    udisks = FakeUDisks(tmp_path)
    keys = FakeKeyslots()
    helper = FakePrivilegedProvisioner()
    secrets = FakeSecrets()
    service = DeviceService(
        udisks=udisks,
        keyslots=keys,
        secrets_store=secrets,
        privileged_provisioner=helper,  # type: ignore[arg-type]
    )

    result = service.setup(
        candidate(),
        name="Privileged",
        main_passphrase=bytearray(b"main"),
        acknowledge_recovery=lambda _value: True,
        automatic_unlock=True,
    )

    assert helper.transaction is not None
    assert helper.transaction.outcomes == ["commit"]
    assert len(helper.host_credentials) == 1
    assert helper.host_credentials[0] in secrets.values.values()
    assert keys.added == []
    assert result.automatic_unlock
    private = read_private_manifest(udisks.private_root)
    assert [item.keyslot for item in private.host_credentials] == [2]


def test_privileged_setup_aborts_inside_the_same_transaction_before_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    helper = FakePrivilegedProvisioner()
    keys = FakeKeyslots()
    service = DeviceService(
        udisks=FakeUDisks(tmp_path),
        keyslots=keys,
        secrets_store=FakeSecrets(),
        privileged_provisioner=helper,  # type: ignore[arg-type]
    )

    with pytest.raises(DeviceError, match="recovery credential confirmation"):
        service.setup(
            candidate(),
            name="Cancelled",
            main_passphrase=bytearray(b"main"),
            acknowledge_recovery=lambda _value: False,
            automatic_unlock=True,
        )

    assert helper.transaction is not None
    assert helper.transaction.outcomes == ["abort"]
    assert helper.host_credentials[0] is not None
    assert keys.removed == []


def test_secret_service_failure_rolls_back_new_host_key_and_keeps_prompt_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    keys = FakeKeyslots()
    service = DeviceService(
        udisks=FakeUDisks(tmp_path),
        keyslots=keys,
        secrets_store=FakeSecrets(fail_store=True),
    )

    result = service.setup(
        candidate(),
        name="Rollback",
        main_passphrase=bytearray(b"main"),
        acknowledge_recovery=lambda _value: True,
        automatic_unlock=True,
    )

    assert keys.slots == {0, 1}
    assert keys.added[-1][1] in keys.removed
    assert not result.automatic_unlock
    assert result.automatic_unlock_error == "mock Secret Service failure"
    assert load_host_devices().devices[result.device_id].unlock_mode is UnlockMode.PROMPT


def test_host_enrollment_uses_unique_credentials_and_lowest_free_slots(tmp_path: Path) -> None:
    keys = FakeKeyslots()
    keys.slots = {0, 1}
    secrets = FakeSecrets()
    service = DeviceService(
        udisks=FakeUDisks(tmp_path), keyslots=keys, secrets_store=secrets
    )
    private = PrivateDeviceManifest(str(uuid.uuid4()))

    first, first_secret = service._enroll_host_credential(
        Path("/dev/private"), "LUKS-UUID", private, bytearray(b"owner")
    )
    second, second_secret = service._enroll_host_credential(
        Path("/dev/private"),
        "LUKS-UUID",
        private.with_host_credential(first),
        bytearray(b"owner"),
    )

    assert (first.keyslot, second.keyslot) == (2, 3)
    assert first.credential_id != second.credential_id
    assert first_secret != second_secret
    assert set(keys.slots) == {0, 1, 2, 3}


def test_replace_public_contents_preserves_the_bound_device_marker(tmp_path: Path) -> None:
    udisks = FakeUDisks(tmp_path)
    device_id = str(uuid.uuid4())
    write_public_manifest(udisks.public_root, PublicDeviceManifest(device_id))
    (udisks.public_root / "old.txt").write_text("old", encoding="utf-8")
    source = tmp_path / "replacement"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "new.txt").write_text("new", encoding="utf-8")
    policy = HostDevicePolicy(device_id, "Drive", "PUBLIC-UUID", "LUKS-UUID")
    udisks.connected = ConnectedDevice(
        "/drive",
        "/public",
        Path("/dev/public"),
        "/encrypted",
        Path("/dev/private"),
        "PUBLIC-UUID",
        "LUKS-UUID",
        None,
    )
    service = DeviceService(
        udisks=udisks, keyslots=FakeKeyslots(), secrets_store=FakeSecrets()
    )

    result = service.replace_public_contents(policy, source)

    assert result.files == 1
    assert read_public_manifest(udisks.public_root).device_id == device_id
    assert (udisks.public_root / "nested" / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (udisks.public_root / "old.txt").exists()


def test_host_enrollment_stops_when_slots_are_exhausted(tmp_path: Path) -> None:
    keys = FakeKeyslots()
    keys.slots = set(range(32))
    service = DeviceService(
        udisks=FakeUDisks(tmp_path), keyslots=keys, secrets_store=FakeSecrets()
    )
    private = PrivateDeviceManifest(
        str(uuid.uuid4()),
        tuple(HostCredential(str(uuid.uuid4()), slot) for slot in range(2, 32)),
    )

    with pytest.raises(DeviceError, match="forget an obsolete trusted host"):
        service._enroll_host_credential(
            Path("/dev/private"), "LUKS-UUID", private, bytearray(b"owner")
        )


def test_enabling_automatic_unlock_preserves_an_existing_open_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    udisks = FakeUDisks(tmp_path)
    keys = FakeKeyslots()
    keys.slots = {0, 1}
    device_id = str(uuid.uuid4())
    write_private_manifest(udisks.private_root, PrivateDeviceManifest(device_id))
    policy = HostDevicePolicy(device_id, "Drive", "PUBLIC-UUID", "LUKS-UUID")
    udisks.connected = ConnectedDevice(
        "/drive",
        "/public",
        Path("/dev/fake-public"),
        "/encrypted",
        Path("/dev/fake-private"),
        "PUBLIC-UUID",
        "LUKS-UUID",
        "/clear",
    )
    service = DeviceService(
        udisks=udisks, keyslots=keys, secrets_store=FakeSecrets()
    )

    updated = service.enable_automatic(policy, bytearray(b"owner"))

    assert updated.unlock_mode is UnlockMode.AUTOMATIC
    assert "unmount:/clear" not in udisks.closed
    assert "lock:/encrypted" not in udisks.closed


def _opened_service(
    tmp_path: Path, *, removed: bool
) -> tuple[DeviceService, HostDevicePolicy, FakeUDisks]:
    udisks = FakeUDisks(tmp_path)
    udisks.removed_on_monitor = removed
    device_id = str(uuid.uuid4())
    Workspace.create(udisks.private_root / "workspace")
    write_private_manifest(udisks.private_root, PrivateDeviceManifest(device_id))
    policy = HostDevicePolicy(device_id, "Drive", "PUBLIC-UUID", "LUKS-UUID")
    udisks.connected = ConnectedDevice(
        "/drive",
        "/public",
        Path("/dev/fake-public"),
        "/encrypted",
        Path("/dev/fake-private"),
        "PUBLIC-UUID",
        "LUKS-UUID",
        "/clear",
    )

    def process_factory(_command: list[str], **_kwargs: Any) -> subprocess.Popen[Any]:
        duration = "60" if removed else "0.05"
        return subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({duration})"],
            start_new_session=True,
        )

    service = DeviceService(
        udisks=udisks,
        keyslots=FakeKeyslots(),
        secrets_store=FakeSecrets(),
        process_factory=process_factory,
    )
    return service, policy, udisks


def test_physical_removal_terminates_shell_and_skips_disk_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    service, policy, udisks = _opened_service(tmp_path, removed=True)

    result = service.open_session(policy, lambda _label: bytearray(b"unused"))

    assert result < 0
    assert udisks.closed == []
    assert not (tmp_path / "runtime/pandoracle/active-device.json").exists()


def test_only_one_opening_session_can_own_a_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    first = _acquire_device_session_lock("device-id")
    try:
        with pytest.raises(DeviceError, match="already has an opening session"):
            _acquire_device_session_lock("device-id")
    finally:
        os.close(first)


def test_normal_shell_exit_flushes_unmounts_locks_and_powers_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    service, policy, udisks = _opened_service(tmp_path, removed=False)

    assert service.open_session(policy, lambda _label: bytearray(b"unused")) == 0
    assert udisks.closed == [
        "unmount:/clear",
        "lock:/encrypted",
        "unmount:/public",
        "power:/drive",
    ]


def test_busy_clean_close_is_not_forced_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    service, policy, udisks = _opened_service(tmp_path, removed=False)
    udisks.unmount_failures["/public"] = 1
    retries: list[str] = []

    result = service.open_session(
        policy,
        lambda _label: bytearray(b"unused"),
        close_retry=retries.append,
    )

    assert result == 0
    assert retries == ["public device area is still in use by another process"]
    assert udisks.unmount_failures["/public"] == 0
    assert udisks.closed[-2:] == ["unmount:/public", "power:/drive"]


def test_physical_removal_ends_busy_close_wait_without_another_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    service, policy, udisks = _opened_service(tmp_path, removed=False)
    udisks.unmount_failures["/public"] = 100
    udisks.remove_after_busy = True
    reports: list[str] = []

    assert service.open_session(
        policy,
        lambda _label: bytearray(b"unused"),
        close_retry=reports.append,
    ) == 0

    assert reports == ["public device area is still in use by another process"]
    assert not (tmp_path / "runtime/pandoracle/active-device.json").exists()


def test_nonbusy_close_failure_is_not_retried_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    service, policy, udisks = _opened_service(tmp_path, removed=False)
    original_unmount = udisks.unmount
    reports: list[str] = []

    def fail_public_unmount(path: str) -> None:
        if path == "/public":
            raise DeviceError("authorization denied")
        original_unmount(path)

    udisks.unmount = fail_public_unmount  # type: ignore[method-assign]

    with pytest.raises(DeviceError, match="authorization denied"):
        service.open_session(
            policy,
            lambda _label: bytearray(b"unused"),
            close_retry=reports.append,
        )

    assert reports == []


def test_busy_close_can_name_a_same_user_process_holding_a_file(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.write_text("synthetic", encoding="utf-8")

    with held.open("rb"):
        holders = _mount_holders(tmp_path)

    assert any(f"PID {os.getpid()}" in holder for holder in holders)


def test_failed_automatic_unlock_prompts_without_saving_owner_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    service, base_policy, udisks = _opened_service(tmp_path, removed=False)
    credential_id = str(uuid.uuid4())
    policy = HostDevicePolicy(
        base_policy.device_id,
        base_policy.name,
        base_policy.public_uuid,
        base_policy.luks_uuid,
        True,
        UnlockMode.AUTOMATIC,
        credential_id,
        2,
    )
    assert udisks.connected is not None
    udisks.connected = ConnectedDevice(
        udisks.connected.drive_path,
        udisks.connected.public_path,
        udisks.connected.public_device,
        udisks.connected.encrypted_path,
        udisks.connected.encrypted_device,
        udisks.connected.public_uuid,
        udisks.connected.luks_uuid,
        None,
    )
    host_secret = b"host-only-secret"
    owner = bytearray(b"owner-passphrase")
    assert isinstance(service.secrets, FakeSecrets)
    service.secrets.values[(policy.device_id, policy.luks_uuid, credential_id)] = host_secret
    udisks.rejected_unlock = host_secret

    assert service.open_session(policy, lambda _label: owner) == 0

    assert udisks.unlock_values == [host_secret, b"owner-passphrase"]
    assert owner == bytearray(len(owner))
    assert list(service.secrets.values.values()) == [host_secret]


def test_forget_absent_deletes_local_policy_and_reports_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    udisks = FakeUDisks(tmp_path)
    credential_id = str(uuid.uuid4())
    policy = HostDevicePolicy(
        str(uuid.uuid4()),
        "Absent",
        "PUBLIC-UUID",
        "LUKS-UUID",
        True,
        UnlockMode.AUTOMATIC,
        credential_id,
        2,
    )
    save_host_devices(HostDeviceConfig(True, {policy.device_id: policy}))
    secrets = FakeSecrets()
    secrets.values[(policy.device_id, policy.luks_uuid, credential_id)] = b"host-secret"
    service = DeviceService(udisks=udisks, keyslots=FakeKeyslots(), secrets_store=secrets)

    result = service.forget(policy)

    assert result.orphaned_key_possible
    assert not result.device_key_removed
    assert not secrets.values
    assert not load_host_devices().devices


def test_connected_forget_revokes_only_matching_host_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    udisks = FakeUDisks(tmp_path)
    device_id = str(uuid.uuid4())
    credential_id = str(uuid.uuid4())
    host = HostCredential(credential_id, 2)
    Workspace.create(udisks.private_root / "workspace")
    write_private_manifest(udisks.private_root, PrivateDeviceManifest(device_id, (host,)))
    policy = HostDevicePolicy(
        device_id,
        "Connected",
        "PUBLIC-UUID",
        "LUKS-UUID",
        True,
        UnlockMode.AUTOMATIC,
        credential_id,
        2,
    )
    udisks.connected = ConnectedDevice(
        "/drive",
        "/public",
        Path("/dev/public"),
        "/encrypted",
        Path("/dev/private"),
        "PUBLIC-UUID",
        "LUKS-UUID",
        "/clear",
    )
    secret = b"host-secret"
    keys = FakeKeyslots()
    keys.slots = {0, 1, 2}
    keys.added.append((b"owner", secret, 2))
    secrets = FakeSecrets()
    secrets.values[(device_id, "LUKS-UUID", credential_id)] = secret
    save_host_devices(HostDeviceConfig(True, {device_id: policy}))
    service = DeviceService(udisks=udisks, keyslots=keys, secrets_store=secrets)

    result = service.forget(policy)

    assert result.device_key_removed
    assert keys.slots == {0, 1}
    assert read_private_manifest(udisks.private_root).host_credentials == ()
    assert not secrets.values
