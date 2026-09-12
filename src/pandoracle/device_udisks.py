from __future__ import annotations

import os
import threading
import time
from ctypes import CDLL, get_errno
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jeepney import DBusAddress, HeaderFields, MatchRule, MessageType, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection

from pandoracle.device_models import (
    PRIVATE_LABEL,
    PUBLIC_LABEL,
    PUBLIC_PARTITION_BYTES,
    HostDevicePolicy,
)
from pandoracle.errors import DeviceBusyError, DeviceError

UDISKS_BUS = "org.freedesktop.UDisks2"
UDISKS_ROOT = "/org/freedesktop/UDisks2"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
BLOCK = "org.freedesktop.UDisks2.Block"
DRIVE = "org.freedesktop.UDisks2.Drive"
PARTITION = "org.freedesktop.UDisks2.Partition"
PARTITION_TABLE = "org.freedesktop.UDisks2.PartitionTable"
FILESYSTEM = "org.freedesktop.UDisks2.Filesystem"
ENCRYPTED = "org.freedesktop.UDisks2.Encrypted"
LINUX_LUKS_GUID = "ca7d7ccb-63ed-4c53-861c-1742536059cc"
MICROSOFT_BASIC_GUID = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"


@dataclass(frozen=True)
class DriveCandidate:
    object_path: str
    block_path: str
    device: Path
    model: str
    serial: str
    size: int
    connection_bus: str

    @property
    def display_name(self) -> str:
        identity = self.model.strip() or "Removable drive"
        suffix = self.serial[-6:] if self.serial else "no serial"
        return f"{identity} — {self.size / 1024**3:.1f} GiB — {suffix}"


@dataclass(frozen=True)
class ConnectedDevice:
    drive_path: str
    public_path: str
    public_device: Path
    encrypted_path: str
    encrypted_device: Path
    public_uuid: str
    luks_uuid: str
    cleartext_path: str | None = None


@dataclass(frozen=True)
class ProvisionedDevice:
    drive_path: str
    public_path: str
    encrypted_path: str
    cleartext_path: str
    public_uuid: str
    luks_uuid: str
    encrypted_device: Path


class UDisksClient:
    def managed_objects(self) -> dict[str, dict[str, dict[str, Any]]]:
        with open_dbus_connection("SYSTEM") as connection:
            address = DBusAddress(UDISKS_ROOT, bus_name=UDISKS_BUS, interface=OBJECT_MANAGER)
            try:
                reply = connection.send_and_get_reply(new_method_call(address, "GetManagedObjects"))
            except Exception as error:
                raise DeviceError(f"cannot query UDisks2: {error}") from error
        return _unwrap(reply.body[0])

    def list_candidates(self) -> list[DriveCandidate]:
        objects = self.managed_objects()
        result: list[DriveCandidate] = []
        for block_path, interfaces in objects.items():
            block = interfaces.get(BLOCK)
            if block is None or PARTITION in interfaces:
                continue
            drive_path = str(block.get("Drive", "/"))
            drive = objects.get(drive_path, {}).get(DRIVE, {})
            connection_bus = str(drive.get("ConnectionBus", ""))
            external = bool(drive.get("Removable") or drive.get("MediaRemovable"))
            external = external or connection_bus in {"usb", "firewire", "sdio"}
            if (
                not external
                or bool(block.get("HintSystem"))
                or bool(block.get("ReadOnly"))
                or not bool(block.get("HintPartitionable", True))
            ):
                continue
            result.append(
                DriveCandidate(
                    drive_path,
                    block_path,
                    _device_path(block["PreferredDevice"]),
                    " ".join(
                        item
                        for item in (str(drive.get("Vendor", "")), str(drive.get("Model", "")))
                        if item
                    ),
                    str(drive.get("Serial", "")),
                    int(block.get("Size", drive.get("Size", 0))),
                    connection_bus,
                )
            )
        return sorted(result, key=lambda item: (item.model, item.serial, str(item.device)))

    def connected_for_policy(self, policy: HostDevicePolicy) -> ConnectedDevice | None:
        objects = self.managed_objects()
        public: tuple[str, dict[str, Any]] | None = None
        encrypted: tuple[str, dict[str, Any]] | None = None
        for path, interfaces in objects.items():
            block = interfaces.get(BLOCK)
            if block is None:
                continue
            if block.get("IdUUID") == policy.public_uuid and block.get("IdLabel") == PUBLIC_LABEL:
                public = (path, block)
            if block.get("IdUUID") == policy.luks_uuid and block.get("IdLabel") == PRIVATE_LABEL:
                encrypted = (path, block)
        if (
            public is None
            or encrypted is None
            or public[1].get("Drive") != encrypted[1].get("Drive")
        ):
            return None
        clear = objects.get(encrypted[0], {}).get(ENCRYPTED, {}).get("CleartextDevice", "/")
        return ConnectedDevice(
            str(public[1]["Drive"]),
            public[0],
            _device_path(public[1]["PreferredDevice"]),
            encrypted[0],
            _device_path(encrypted[1]["PreferredDevice"]),
            policy.public_uuid,
            policy.luks_uuid,
            None if clear == "/" else str(clear),
        )

    def probable_devices(self) -> list[ConnectedDevice]:
        objects = self.managed_objects()
        by_drive: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        for path, interfaces in objects.items():
            block = interfaces.get(BLOCK)
            if block is None:
                continue
            label = str(block.get("IdLabel", ""))
            if label in {PUBLIC_LABEL, PRIVATE_LABEL}:
                by_drive.setdefault(str(block.get("Drive", "/")), {})[label] = (path, block)
        result = []
        for drive_path, pair in by_drive.items():
            if set(pair) != {PUBLIC_LABEL, PRIVATE_LABEL}:
                continue
            public_path, public = pair[PUBLIC_LABEL]
            encrypted_path, encrypted = pair[PRIVATE_LABEL]
            clear = objects.get(encrypted_path, {}).get(ENCRYPTED, {}).get("CleartextDevice", "/")
            result.append(
                ConnectedDevice(
                    drive_path,
                    public_path,
                    _device_path(public["PreferredDevice"]),
                    encrypted_path,
                    _device_path(encrypted["PreferredDevice"]),
                    str(public.get("IdUUID", "")),
                    str(encrypted.get("IdUUID", "")),
                    None if clear == "/" else str(clear),
                )
            )
        return result

    def provision(
        self, candidate: DriveCandidate, passphrase: bytes | bytearray
    ) -> ProvisionedDevice:
        current = {item.block_path: item for item in self.list_candidates()}
        if candidate.block_path not in current or current[candidate.block_path] != candidate:
            raise DeviceError("selected removable drive changed before provisioning")
        stage = "erasing the drive"
        try:
            self._unmount_existing_partitions(candidate.block_path)
            self._call(
                candidate.block_path,
                BLOCK,
                "Format",
                "sa{sv}",
                ("gpt", {"tear-down": ("b", True)}),
            )
            self._wait_for_interface(
                candidate.block_path,
                PARTITION_TABLE,
                drive_path=candidate.object_path,
                stage=stage,
                properties={"Type": "gpt"},
            )
            stage = "creating the public filesystem"
            public_path = self._call(
                candidate.block_path,
                PARTITION_TABLE,
                "CreatePartitionAndFormat",
                "ttssa{sv}sa{sv}",
                (
                    1024 * 1024,
                    PUBLIC_PARTITION_BYTES,
                    MICROSOFT_BASIC_GUID,
                    "PANDORA_PUBLIC",
                    {},
                    "vfat",
                    {
                        "label": ("s", PUBLIC_LABEL),
                        "update-partition-type": ("b", True),
                    },
                ),
            )[0]
            stage = "creating the private encrypted filesystem"
            private_path = self._call(
                candidate.block_path,
                PARTITION_TABLE,
                "CreatePartitionAndFormat",
                "ttssa{sv}sa{sv}",
                (
                    PUBLIC_PARTITION_BYTES + 2 * 1024 * 1024,
                    0,
                    LINUX_LUKS_GUID,
                    "PANDORA_PRIVATE",
                    {},
                    "ext4",
                    {
                        "label": ("s", PRIVATE_LABEL),
                        "take-ownership": ("b", True),
                        "encrypt.type": ("s", "luks2"),
                        "encrypt.label": ("s", PRIVATE_LABEL),
                        "encrypt.passphrase": ("ay", bytes(passphrase)),
                        "update-partition-type": ("b", True),
                    },
                ),
            )[0]
            stage = "publishing the public filesystem"
            self._wait_for_interface(
                public_path,
                FILESYSTEM,
                drive_path=candidate.object_path,
                stage=stage,
            )
            stage = "publishing the encrypted filesystem"
            self._wait_for_interface(
                private_path,
                ENCRYPTED,
                drive_path=candidate.object_path,
                stage=stage,
            )
            objects = self.managed_objects()
            encrypted = objects[private_path][BLOCK]
            clear = str(objects[private_path][ENCRYPTED].get("CleartextDevice", "/"))
            if clear == "/":
                raise DeviceError(
                    "UDisks2 did not leave the new private filesystem unlocked"
                )
            stage = "publishing the private filesystem"
            self._wait_for_interface(
                clear,
                FILESYSTEM,
                drive_path=candidate.object_path,
                stage=stage,
            )
            public = (
                objects.get(public_path, {}).get(BLOCK)
                or self.managed_objects()[public_path][BLOCK]
            )
        except (DeviceError, KeyError) as error:
            self._raise_if_drive_disconnected(candidate.object_path, stage, error)
            raise
        return ProvisionedDevice(
            candidate.object_path,
            str(public_path),
            str(private_path),
            clear,
            str(public["IdUUID"]),
            str(encrypted["IdUUID"]),
            _device_path(encrypted["PreferredDevice"]),
        )

    def unlock(self, encrypted_path: str, credential: bytes | bytearray | str) -> str:
        existing = self.cleartext_path(encrypted_path)
        if existing is not None:
            return existing
        value = credential if isinstance(credential, str) else bytes(credential).decode("ascii")
        try:
            return str(
                self._call(encrypted_path, ENCRYPTED, "Unlock", "sa{sv}", (value, {}))[0]
            )
        except DeviceError:
            # Another opener can win between the property read and Unlock().
            # Treat the resulting "already in use" state as success only when
            # UDisks now publishes the exact mapping we intended to open.
            existing = self.cleartext_path(encrypted_path)
            if existing is not None:
                return existing
            raise

    def mount(self, filesystem_path: str) -> Path:
        objects = self.managed_objects()
        mounts = objects.get(filesystem_path, {}).get(FILESYSTEM, {}).get("MountPoints", [])
        if mounts:
            return _device_path(mounts[0])
        try:
            path = self._call(filesystem_path, FILESYSTEM, "Mount", "a{sv}", ({},))[0]
        except DeviceError:
            # Desktop automounters can win between the property read and Mount().
            # Accept that race only when UDisks now publishes a mount point for
            # this exact filesystem object.
            mounts = (
                self.managed_objects()
                .get(filesystem_path, {})
                .get(FILESYSTEM, {})
                .get("MountPoints", [])
            )
            if mounts:
                return _device_path(mounts[0])
            raise
        return Path(str(path))

    def mount_points(self, filesystem_path: str) -> list[Path]:
        if not filesystem_path:
            return []
        mounts = (
            self.managed_objects()
            .get(filesystem_path, {})
            .get(FILESYSTEM, {})
            .get("MountPoints", [])
        )
        return [_device_path(item) for item in mounts]

    def cleartext_path(self, encrypted_path: str) -> str | None:
        value = (
            self.managed_objects()
            .get(encrypted_path, {})
            .get(ENCRYPTED, {})
            .get("CleartextDevice", "/")
        )
        return None if value == "/" else str(value)

    def unmount(self, filesystem_path: str) -> None:
        if not self.mount_points(filesystem_path):
            return
        try:
            self._call(
                filesystem_path,
                FILESYSTEM,
                "Unmount",
                "a{sv}",
                ({"force": ("b", False)},),
            )
        except DeviceError:
            # Treat a concurrent desktop/user unmount as success, but preserve
            # every failure while this exact filesystem is still mounted.
            if not self.mount_points(filesystem_path):
                return
            raise

    def lock(self, encrypted_path: str) -> None:
        if self.cleartext_path(encrypted_path) is None:
            return
        try:
            self._call(encrypted_path, ENCRYPTED, "Lock", "a{sv}", ({},))
        except DeviceError:
            # A concurrent locker can retire the mapping between the property
            # read and Lock(). Only that observed end state is success.
            if self.cleartext_path(encrypted_path) is None:
                return
            raise

    def wait_until_locked(self, encrypted_path: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cleartext_path(encrypted_path) is None:
                return
            time.sleep(0.1)
        raise DeviceError("UDisks2 did not retire the temporary cleartext mapping")

    def take_ownership(self, filesystem_path: str) -> None:
        self._call(
            filesystem_path,
            FILESYSTEM,
            "TakeOwnership",
            "a{sv}",
            ({"recursive": ("b", True)},),
        )

    def power_off(self, drive_path: str) -> None:
        self._call(drive_path, DRIVE, "PowerOff", "a{sv}", ({},))

    def is_present(self, paths: set[str]) -> bool:
        objects = self.managed_objects()
        return all(path in objects for path in paths)

    def monitor_removal(
        self,
        session_paths: set[str],
        drive_path: str,
        closing: threading.Event,
        removed: threading.Event,
        stopped: threading.Event,
    ) -> None:
        subscription = MatchRule(
            type="signal",
            sender=UDISKS_BUS,
            path_namespace=UDISKS_ROOT,
        )
        local_rule = MatchRule(type="signal", path_namespace=UDISKS_ROOT)
        try:
            with open_dbus_connection("SYSTEM") as connection:
                connection.send_and_get_reply(message_bus.AddMatch(subscription))
                with connection.filter(local_rule, bufsize=64) as matches:
                    while not stopped.is_set():
                        try:
                            connection.recv_until_filtered(matches, timeout=0.5)
                        except TimeoutError:
                            continue
                        required = {drive_path} if closing.is_set() else session_paths
                        if not self.is_present(required):
                            removed.set()
                            return
        except Exception:
            # A lost UDisks connection is treated as lost device supervision.
            removed.set()

    def monitor_events(self, callback: Any, stopped: threading.Event) -> None:
        subscription = MatchRule(
            type="signal", sender=UDISKS_BUS, path_namespace=UDISKS_ROOT
        )
        local_rule = MatchRule(type="signal", path_namespace=UDISKS_ROOT)
        try:
            with open_dbus_connection("SYSTEM") as connection:
                connection.send_and_get_reply(message_bus.AddMatch(subscription))
                with connection.filter(local_rule, bufsize=64) as matches:
                    while not stopped.is_set():
                        try:
                            connection.recv_until_filtered(matches, timeout=0.5)
                        except TimeoutError:
                            continue
                        callback()
        except Exception as error:
            raise DeviceError(f"lost UDisks2 event monitor: {error}") from error

    def _call(
        self,
        path: str,
        interface: str,
        method: str,
        signature: str | None = None,
        body: tuple[Any, ...] = (),
    ) -> tuple[Any, ...]:
        address = DBusAddress(path, bus_name=UDISKS_BUS, interface=interface)
        try:
            with open_dbus_connection("SYSTEM") as connection:
                reply = connection.send_and_get_reply(
                    new_method_call(address, method, signature, body), timeout=300
                )
            if reply.header.message_type is MessageType.error:
                error_name = str(
                    reply.header.fields.get(HeaderFields.error_name, "D-Bus error")
                )
                detail = str(reply.body[0]) if reply.body else error_name
                error_type = (
                    DeviceBusyError
                    if error_name == "org.freedesktop.UDisks2.Error.DeviceBusy"
                    else DeviceError
                )
                raise error_type(f"UDisks2 {method} failed: {detail} ({error_name})")
            return tuple(_unwrap(item) for item in reply.body)
        except DeviceError:
            raise
        except Exception as error:
            raise DeviceError(f"UDisks2 {method} failed: {error}") from error

    def _unmount_existing_partitions(self, table_path: str) -> None:
        objects = self.managed_objects()
        partitions = objects.get(table_path, {}).get(PARTITION_TABLE, {}).get(
            "Partitions", []
        )
        for partition_path in partitions:
            path = str(partition_path)
            interfaces = objects.get(path, {})
            filesystem = interfaces.get(FILESYSTEM, {})
            if filesystem.get("MountPoints"):
                self.unmount(path)

            cleartext = str(interfaces.get(ENCRYPTED, {}).get("CleartextDevice", "/"))
            if cleartext == "/":
                continue
            if (
                objects.get(cleartext, {})
                .get(FILESYSTEM, {})
                .get("MountPoints")
            ):
                self.unmount(cleartext)
            self.lock(path)

    def _wait_for_interface(
        self,
        path: str,
        interface: str,
        timeout: float = 30.0,
        *,
        drive_path: str | None = None,
        stage: str = "preparing the device",
        properties: dict[str, Any] | None = None,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            objects = self.managed_objects()
            if drive_path is not None and DRIVE not in objects.get(drive_path, {}):
                self._raise_drive_disconnected(stage)
            published = objects.get(path, {}).get(interface)
            if published is not None and all(
                published.get(name) == expected
                for name, expected in (properties or {}).items()
            ):
                return
            time.sleep(0.1)
        raise DeviceError(
            f"UDisks2 did not publish {interface} for {path} while {stage}"
        )

    def _raise_if_drive_disconnected(
        self, drive_path: str, stage: str, cause: BaseException
    ) -> None:
        try:
            present = DRIVE in self.managed_objects().get(drive_path, {})
        except DeviceError:
            return
        if not present:
            self._raise_drive_disconnected(stage, cause)

    @staticmethod
    def _raise_drive_disconnected(
        stage: str, cause: BaseException | None = None
    ) -> None:
        raise DeviceError(
            f"Pandora drive disconnected while {stage}. Reconnect it, make sure the "
            "USB connection is stable, and rerun 'pandoracle device setup'; the "
            "incomplete layout will be replaced."
        ) from cause


def sync_filesystem(root: Path) -> None:
    try:
        descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            libc = CDLL(None, use_errno=True)
            if libc.syncfs(descriptor) != 0:
                error_number = get_errno()
                raise OSError(error_number, os.strerror(error_number))
        finally:
            os.close(descriptor)
    except OSError as error:
        raise DeviceError(f"could not flush private filesystem: {error}") from error


def _unwrap(value: Any) -> Any:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return _unwrap(value[1])
    if isinstance(value, dict):
        return {key: _unwrap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwrap(item) for item in value]
    return value


def _device_path(value: Any) -> Path:
    if isinstance(value, bytes):
        value = value.rstrip(b"\x00").decode("utf-8")
    return Path(str(value))
