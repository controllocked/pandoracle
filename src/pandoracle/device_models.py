from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pandoracle.config import config_path
from pandoracle.errors import DeviceError
from pandoracle.fs import write_json_atomic

DEVICE_FORMAT_VERSION = 1
HOST_CONFIG_VERSION = 1
PUBLIC_LABEL = "PANDORA_PUB"
PRIVATE_LABEL = "PANDORA_PRIVATE"
PUBLIC_PARTITION_BYTES = 64 * 1024 * 1024
PUBLIC_MARKER = Path(".pandoracle-device/device.json")
PRIVATE_MARKER = Path(".pandoracle-device.json")
WORKSPACE_DIRECTORY = Path("workspace")


class UnlockMode(StrEnum):
    PROMPT = "prompt"
    AUTOMATIC = "automatic"


@dataclass(frozen=True)
class HostCredential:
    credential_id: str
    keyslot: int

    def __post_init__(self) -> None:
        _uuid(self.credential_id, "credential ID")
        if not 2 <= self.keyslot <= 31:
            raise DeviceError("host credential keyslot must be between 2 and 31")

    def to_dict(self) -> dict[str, Any]:
        return {"credential_id": self.credential_id, "keyslot": self.keyslot}

    @classmethod
    def from_dict(cls, value: object) -> HostCredential:
        data = _object(value, {"credential_id", "keyslot"}, "host credential")
        return cls(str(data["credential_id"]), int(data["keyslot"]))


@dataclass(frozen=True)
class PublicDeviceManifest:
    device_id: str
    device_format_version: int = DEVICE_FORMAT_VERSION

    def __post_init__(self) -> None:
        _uuid(self.device_id, "device ID")
        if self.device_format_version != DEVICE_FORMAT_VERSION:
            raise DeviceError("unsupported Pandora public-device format")

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_format_version": self.device_format_version,
            "device_id": self.device_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> PublicDeviceManifest:
        data = _object(value, {"device_format_version", "device_id"}, "public marker")
        return cls(str(data["device_id"]), int(data["device_format_version"]))


@dataclass(frozen=True)
class PrivateDeviceManifest:
    device_id: str
    host_credentials: tuple[HostCredential, ...] = ()
    device_format_version: int = DEVICE_FORMAT_VERSION
    main_keyslot: int = 0
    recovery_keyslot: int = 1
    workspace: str = str(WORKSPACE_DIRECTORY)

    def __post_init__(self) -> None:
        _uuid(self.device_id, "device ID")
        if self.device_format_version != DEVICE_FORMAT_VERSION:
            raise DeviceError("unsupported Pandora private-device format")
        if self.main_keyslot != 0 or self.recovery_keyslot != 1:
            raise DeviceError("invalid reserved Pandora keyslots")
        if self.workspace != str(WORKSPACE_DIRECTORY):
            raise DeviceError("invalid private workspace location")
        ids = [item.credential_id for item in self.host_credentials]
        slots = [item.keyslot for item in self.host_credentials]
        if len(ids) != len(set(ids)) or len(slots) != len(set(slots)):
            raise DeviceError("duplicate host credential metadata")

    def next_host_keyslot(self) -> int:
        used = {0, 1, *(item.keyslot for item in self.host_credentials)}
        for slot in range(2, 32):
            if slot not in used:
                return slot
        raise DeviceError(
            "automatic unlock has no free LUKS2 keyslot; forget an obsolete trusted host"
        )

    def with_host_credential(self, credential: HostCredential) -> PrivateDeviceManifest:
        return PrivateDeviceManifest(
            self.device_id,
            tuple((*self.host_credentials, credential)),
            self.device_format_version,
            self.main_keyslot,
            self.recovery_keyslot,
            self.workspace,
        )

    def without_host_credential(self, credential_id: str) -> PrivateDeviceManifest:
        return PrivateDeviceManifest(
            self.device_id,
            tuple(item for item in self.host_credentials if item.credential_id != credential_id),
            self.device_format_version,
            self.main_keyslot,
            self.recovery_keyslot,
            self.workspace,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_format_version": self.device_format_version,
            "device_id": self.device_id,
            "credentials": {
                "main_keyslot": self.main_keyslot,
                "recovery_keyslot": self.recovery_keyslot,
                "host_credentials": [item.to_dict() for item in self.host_credentials],
            },
            "workspace": self.workspace,
        }

    @classmethod
    def from_dict(cls, value: object) -> PrivateDeviceManifest:
        data = _object(
            value,
            {"device_format_version", "device_id", "credentials", "workspace"},
            "private marker",
        )
        credentials = _object(
            data["credentials"],
            {"main_keyslot", "recovery_keyslot", "host_credentials"},
            "credential metadata",
        )
        items = credentials["host_credentials"]
        if not isinstance(items, list):
            raise DeviceError("invalid private marker: host_credentials must be a list")
        return cls(
            str(data["device_id"]),
            tuple(HostCredential.from_dict(item) for item in items),
            int(data["device_format_version"]),
            int(credentials["main_keyslot"]),
            int(credentials["recovery_keyslot"]),
            str(data["workspace"]),
        )


@dataclass(frozen=True)
class HostDevicePolicy:
    device_id: str
    name: str
    public_uuid: str
    luks_uuid: str
    auto_open: bool = True
    unlock_mode: UnlockMode = UnlockMode.PROMPT
    credential_id: str | None = None
    credential_keyslot: int | None = None

    def __post_init__(self) -> None:
        _uuid(self.device_id, "device ID")
        if not self.name.strip() or not self.public_uuid or not self.luks_uuid:
            raise DeviceError("host device policy has an empty required field")
        if self.unlock_mode is UnlockMode.AUTOMATIC:
            if self.credential_id is None or self.credential_keyslot is None:
                raise DeviceError("automatic unlock policy is missing its host credential")
            HostCredential(self.credential_id, self.credential_keyslot)
        elif self.credential_id is not None or self.credential_keyslot is not None:
            raise DeviceError("prompt unlock policy must not reference a host credential")

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "public_uuid": self.public_uuid,
            "luks_uuid": self.luks_uuid,
            "auto_open": self.auto_open,
            "unlock_mode": self.unlock_mode.value,
            "credential_id": self.credential_id,
            "credential_keyslot": self.credential_keyslot,
        }

    @classmethod
    def from_dict(cls, value: object) -> HostDevicePolicy:
        data = _object(
            value,
            {
                "device_id",
                "name",
                "public_uuid",
                "luks_uuid",
                "auto_open",
                "unlock_mode",
                "credential_id",
                "credential_keyslot",
            },
            "host device policy",
        )
        return cls(
            str(data["device_id"]),
            str(data["name"]),
            str(data["public_uuid"]),
            str(data["luks_uuid"]),
            bool(data["auto_open"]),
            UnlockMode(str(data["unlock_mode"])),
            None if data["credential_id"] is None else str(data["credential_id"]),
            None if data["credential_keyslot"] is None else int(data["credential_keyslot"]),
        )


@dataclass
class HostDeviceConfig:
    detection_enabled: bool = False
    devices: dict[str, HostDevicePolicy] = field(default_factory=dict)
    host_config_version: int = HOST_CONFIG_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_config_version": self.host_config_version,
            "detection_enabled": self.detection_enabled,
            "devices": [self.devices[key].to_dict() for key in sorted(self.devices)],
        }

    @classmethod
    def from_dict(cls, value: object) -> HostDeviceConfig:
        data = _object(
            value, {"host_config_version", "detection_enabled", "devices"}, "host config"
        )
        if int(data["host_config_version"]) != HOST_CONFIG_VERSION:
            raise DeviceError("unsupported Pandora host-device config")
        items = data["devices"]
        if not isinstance(items, list):
            raise DeviceError("invalid Pandora host config: devices must be a list")
        policies = [HostDevicePolicy.from_dict(item) for item in items]
        if len({item.device_id for item in policies}) != len(policies):
            raise DeviceError("duplicate device ID in Pandora host config")
        return cls(bool(data["detection_enabled"]), {item.device_id: item for item in policies})


def devices_config_path() -> Path:
    return config_path().with_name("devices.json")


def load_host_devices() -> HostDeviceConfig:
    path = devices_config_path()
    if not path.exists():
        return HostDeviceConfig()
    try:
        return HostDeviceConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, DeviceError):
            raise
        raise DeviceError(f"invalid Pandora host-device config {path}: {error}") from error


def save_host_devices(config: HostDeviceConfig) -> None:
    path = devices_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        write_json_atomic(path, config.to_dict())
    except OSError as error:
        raise DeviceError(f"cannot save Pandora host-device config: {error}") from error


def read_public_manifest(root: Path) -> PublicDeviceManifest:
    return _read_manifest(root / PUBLIC_MARKER, PublicDeviceManifest.from_dict)


def read_private_manifest(root: Path) -> PrivateDeviceManifest:
    return _read_manifest(root / PRIVATE_MARKER, PrivateDeviceManifest.from_dict)


def write_public_manifest(root: Path, manifest: PublicDeviceManifest) -> None:
    target = root / PUBLIC_MARKER
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(target, manifest.to_dict())
    except OSError as error:
        raise DeviceError(f"cannot write Pandora public marker: {error}") from error


def write_private_manifest(root: Path, manifest: PrivateDeviceManifest) -> None:
    try:
        write_json_atomic(root / PRIVATE_MARKER, manifest.to_dict())
    except OSError as error:
        raise DeviceError(f"cannot write Pandora private marker: {error}") from error


def _read_manifest(path: Path, factory: Any) -> Any:
    if path.is_symlink():
        raise DeviceError(f"Pandora marker must not be a symlink: {path}")
    try:
        return factory(json.loads(path.read_text(encoding="utf-8")))
    except DeviceError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DeviceError(f"invalid Pandora marker {path}: {error}") from error


def _object(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise DeviceError(f"invalid {label}: unknown or missing fields")
    return value


def _uuid(value: str, label: str) -> None:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise DeviceError(f"invalid {label}") from error
