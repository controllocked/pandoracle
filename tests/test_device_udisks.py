from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from jeepney import HeaderFields, MessageType

from pandoracle.device_models import HostDevicePolicy
from pandoracle.device_udisks import (
    BLOCK,
    DRIVE,
    ENCRYPTED,
    FILESYSTEM,
    PARTITION,
    PARTITION_TABLE,
    UDisksClient,
)
from pandoracle.errors import DeviceBusyError, DeviceError


def test_candidate_discovery_accepts_blank_or_partitioned_removable_whole_drives() -> None:
    drive = "/org/freedesktop/UDisks2/drives/usb"
    whole_blank = "/org/freedesktop/UDisks2/block_devices/sdz"
    whole_partitioned = "/org/freedesktop/UDisks2/block_devices/sdy"
    partition = f"{whole_partitioned}1"
    objects = {
        drive: {
            DRIVE: {
                "ConnectionBus": "usb",
                "Removable": True,
                "Vendor": "Synthetic",
                "Model": "Drive",
                "Serial": "SERIAL",
                "Size": 8 * 1024**3,
            }
        },
        whole_blank: {
            BLOCK: {
                "Drive": drive,
                "PreferredDevice": b"/dev/sdz\x00",
                "Size": 8 * 1024**3,
                "HintPartitionable": True,
                "HintSystem": False,
                "ReadOnly": False,
            }
        },
        whole_partitioned: {
            BLOCK: {
                "Drive": drive,
                "PreferredDevice": b"/dev/sdy\x00",
                "Size": 8 * 1024**3,
                "HintPartitionable": True,
                "HintSystem": False,
                "ReadOnly": False,
            },
            PARTITION_TABLE: {"Type": "gpt"},
        },
        partition: {
            BLOCK: {"Drive": drive, "PreferredDevice": b"/dev/sdy1\x00"},
            PARTITION: {"Number": 1},
        },
    }
    client = UDisksClient()
    client.managed_objects = lambda: objects  # type: ignore[method-assign]

    candidates = client.list_candidates()

    assert {item.device for item in candidates} == {Path("/dev/sdy"), Path("/dev/sdz")}


def test_connected_policy_requires_public_and_luks_uuids_on_the_same_drive() -> None:
    device_id = "2317ab7f-8ec5-41f2-b25a-55e42fc4d9e6"
    policy = HostDevicePolicy(device_id, "Drive", "PUB-UUID", "LUKS-UUID")
    drive = "/org/freedesktop/UDisks2/drives/usb"
    public = "/org/freedesktop/UDisks2/block_devices/sdz1"
    encrypted = "/org/freedesktop/UDisks2/block_devices/sdz2"
    clear = "/org/freedesktop/UDisks2/block_devices/dm_0"
    objects = {
        public: {
            BLOCK: {
                "Drive": drive,
                "PreferredDevice": b"/dev/sdz1\x00",
                "IdUUID": "PUB-UUID",
                "IdLabel": "PANDORA_PUB",
            },
            FILESYSTEM: {"MountPoints": []},
        },
        encrypted: {
            BLOCK: {
                "Drive": drive,
                "PreferredDevice": b"/dev/sdz2\x00",
                "IdUUID": "LUKS-UUID",
                "IdLabel": "PANDORA_PRIVATE",
            },
            ENCRYPTED: {"CleartextDevice": clear},
        },
        clear: {FILESYSTEM: {"MountPoints": []}},
    }
    client = UDisksClient()
    client.managed_objects = lambda: objects  # type: ignore[method-assign]

    connected = client.connected_for_policy(policy)

    assert connected is not None
    assert connected.drive_path == drive
    assert connected.public_device == Path("/dev/sdz1")
    assert connected.encrypted_device == Path("/dev/sdz2")
    assert connected.cleartext_path == clear


def test_unlock_returns_an_existing_cleartext_mapping_without_another_call() -> None:
    encrypted = "/org/freedesktop/UDisks2/block_devices/sdz2"
    clear = "/org/freedesktop/UDisks2/block_devices/dm_0"
    client = UDisksClient()
    client.managed_objects = lambda: {  # type: ignore[method-assign]
        encrypted: {ENCRYPTED: {"CleartextDevice": clear}}
    }
    client._call = lambda *_args, **_kwargs: pytest.fail("unexpected Unlock call")  # type: ignore[method-assign]

    assert client.unlock(encrypted, bytearray(b"unused")) == clear


def test_unlock_accepts_a_mapping_published_by_a_concurrent_opener() -> None:
    encrypted = "/org/freedesktop/UDisks2/block_devices/sdz2"
    clear = "/org/freedesktop/UDisks2/block_devices/dm_0"
    snapshots = iter(
        [
            {encrypted: {ENCRYPTED: {"CleartextDevice": "/"}}},
            {encrypted: {ENCRYPTED: {"CleartextDevice": clear}}},
        ]
    )
    client = UDisksClient()
    client.managed_objects = lambda: next(snapshots)  # type: ignore[method-assign]

    def already_open(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise DeviceError("UDisks2 Unlock failed: device is already in use")

    client._call = already_open  # type: ignore[method-assign]

    assert client.unlock(encrypted, bytearray(b"unused")) == clear


def test_mount_returns_an_existing_mount_point_without_another_call() -> None:
    filesystem = "/org/freedesktop/UDisks2/block_devices/dm_0"
    mount_point = b"/run/media/user/PANDORA_PRIVATE\x00"
    client = UDisksClient()
    client.managed_objects = lambda: {  # type: ignore[method-assign]
        filesystem: {FILESYSTEM: {"MountPoints": [mount_point]}}
    }
    client._call = lambda *_args, **_kwargs: pytest.fail("unexpected Mount call")  # type: ignore[method-assign]

    assert client.mount(filesystem) == Path("/run/media/user/PANDORA_PRIVATE")


def test_mount_accepts_a_mount_point_published_by_a_concurrent_automounter() -> None:
    filesystem = "/org/freedesktop/UDisks2/block_devices/dm_0"
    mount_point = b"/run/media/user/PANDORA_PRIVATE\x00"
    snapshots = iter(
        [
            {filesystem: {FILESYSTEM: {"MountPoints": []}}},
            {filesystem: {FILESYSTEM: {"MountPoints": [mount_point]}}},
        ]
    )
    client = UDisksClient()
    client.managed_objects = lambda: next(snapshots)  # type: ignore[method-assign]

    def already_mounted(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise DeviceError("UDisks2 Mount failed: device is already mounted")

    client._call = already_mounted  # type: ignore[method-assign]

    assert client.mount(filesystem) == Path("/run/media/user/PANDORA_PRIVATE")


def test_unmount_accepts_a_concurrent_unmount_of_the_same_filesystem() -> None:
    filesystem = "/org/freedesktop/UDisks2/block_devices/dm_0"
    snapshots = iter(
        [
            {filesystem: {FILESYSTEM: {"MountPoints": [b"/media/private\x00"]}}},
            {filesystem: {FILESYSTEM: {"MountPoints": []}}},
        ]
    )
    client = UDisksClient()
    client.managed_objects = lambda: next(snapshots)  # type: ignore[method-assign]

    def already_unmounted(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise DeviceError("filesystem is no longer mounted")

    client._call = already_unmounted  # type: ignore[method-assign]

    client.unmount(filesystem)


def test_lock_accepts_a_mapping_retired_by_a_concurrent_locker() -> None:
    encrypted = "/org/freedesktop/UDisks2/block_devices/sdz2"
    clear = "/org/freedesktop/UDisks2/block_devices/dm_0"
    states = iter((clear, None))
    client = UDisksClient()
    client.cleartext_path = lambda _path: next(states)  # type: ignore[method-assign]

    def already_locked(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise DeviceError("encrypted device is already locked")

    client._call = already_locked  # type: ignore[method-assign]

    client.lock(encrypted)


def test_wait_until_locked_tolerates_delayed_mapping_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UDisksClient()
    states = iter(("/clear", None))
    client.cleartext_path = lambda _path: next(states)  # type: ignore[method-assign]
    monkeypatch.setattr("pandoracle.device_udisks.time.sleep", lambda _seconds: None)

    client.wait_until_locked("/encrypted", timeout=1)


def test_wait_for_interface_reports_physical_drive_disconnection() -> None:
    drive = "/org/freedesktop/UDisks2/drives/usb"
    partition = "/org/freedesktop/UDisks2/block_devices/sdz1"
    client = UDisksClient()
    client.managed_objects = lambda: {partition: {BLOCK: {}}}  # type: ignore[method-assign]

    with pytest.raises(DeviceError, match="drive disconnected while creating"):
        client._wait_for_interface(  # noqa: SLF001
            partition,
            FILESYSTEM,
            timeout=0.1,
            drive_path=drive,
            stage="creating the public filesystem",
        )


def test_wait_for_interface_tolerates_delayed_udisks_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = "/org/freedesktop/UDisks2/drives/usb"
    partition = "/org/freedesktop/UDisks2/block_devices/sdz1"
    snapshots = iter(
        [
            {drive: {DRIVE: {}}, partition: {BLOCK: {}}},
            {drive: {DRIVE: {}}, partition: {BLOCK: {}, FILESYSTEM: {}}},
        ]
    )
    client = UDisksClient()
    client.managed_objects = lambda: next(snapshots)  # type: ignore[method-assign]
    monkeypatch.setattr("pandoracle.device_udisks.time.sleep", lambda _seconds: None)

    client._wait_for_interface(  # noqa: SLF001
        partition,
        FILESYSTEM,
        timeout=1,
        drive_path=drive,
        stage="publishing the public filesystem",
    )


def test_wait_for_interface_rejects_stale_partition_table_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = "/org/freedesktop/UDisks2/drives/usb"
    block = "/org/freedesktop/UDisks2/block_devices/sdz"
    snapshots = iter(
        [
            {drive: {DRIVE: {}}, block: {PARTITION_TABLE: {"Type": "dos"}}},
            {drive: {DRIVE: {}}, block: {PARTITION_TABLE: {"Type": "gpt"}}},
        ]
    )
    client = UDisksClient()
    client.managed_objects = lambda: next(snapshots)  # type: ignore[method-assign]
    monkeypatch.setattr("pandoracle.device_udisks.time.sleep", lambda _seconds: None)

    client._wait_for_interface(  # noqa: SLF001
        block,
        PARTITION_TABLE,
        timeout=1,
        drive_path=drive,
        stage="erasing the drive",
        properties={"Type": "gpt"},
    )


def test_existing_mounted_partitions_are_cleanly_torn_down() -> None:
    table = "/org/freedesktop/UDisks2/block_devices/sdz"
    public = f"{table}1"
    encrypted = f"{table}2"
    clear = "/org/freedesktop/UDisks2/block_devices/dm_0"
    objects = {
        table: {PARTITION_TABLE: {"Partitions": [public, encrypted]}},
        public: {FILESYSTEM: {"MountPoints": [b"/media/public\x00"]}},
        encrypted: {ENCRYPTED: {"CleartextDevice": clear}},
        clear: {FILESYSTEM: {"MountPoints": [b"/media/private\x00"]}},
    }
    client = UDisksClient()
    client.managed_objects = lambda: objects  # type: ignore[method-assign]
    unmounted: list[str] = []
    locked: list[str] = []
    client.unmount = unmounted.append  # type: ignore[method-assign]
    client.lock = locked.append  # type: ignore[method-assign]

    client._unmount_existing_partitions(table)  # noqa: SLF001

    assert unmounted == [public, clear]
    assert locked == [encrypted]


def test_call_turns_dbus_error_reply_into_device_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = SimpleNamespace(
        header=SimpleNamespace(
            message_type=MessageType.error,
            fields={HeaderFields.error_name: "org.example.Error.Busy"},
        ),
        body=("Device or resource busy",),
    )

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def send_and_get_reply(self, *_args: object, **_kwargs: object) -> object:
            return reply

    monkeypatch.setattr(
        "pandoracle.device_udisks.open_dbus_connection", lambda _bus: FakeConnection()
    )

    with pytest.raises(DeviceError, match="Device or resource busy"):
        UDisksClient()._call(  # noqa: SLF001
            "/org/freedesktop/UDisks2/block_devices/sdz",
            BLOCK,
            "Format",
        )


def test_call_preserves_udisks_device_busy_as_a_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = SimpleNamespace(
        header=SimpleNamespace(
            message_type=MessageType.error,
            fields={
                HeaderFields.error_name: "org.freedesktop.UDisks2.Error.DeviceBusy"
            },
        ),
        body=("target is busy",),
    )

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def send_and_get_reply(self, *_args: object, **_kwargs: object) -> object:
            return reply

    monkeypatch.setattr(
        "pandoracle.device_udisks.open_dbus_connection", lambda _bus: FakeConnection()
    )

    with pytest.raises(DeviceBusyError, match="target is busy"):
        UDisksClient()._call(  # noqa: SLF001
            "/org/freedesktop/UDisks2/block_devices/dm_0",
            FILESYSTEM,
            "Unmount",
        )
