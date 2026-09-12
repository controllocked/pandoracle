from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from pandoracle.device_models import (
    HostCredential,
    HostDeviceConfig,
    HostDevicePolicy,
    PrivateDeviceManifest,
    PublicDeviceManifest,
    UnlockMode,
    load_host_devices,
    save_host_devices,
)
from pandoracle.errors import DeviceError


def test_device_descriptors_separate_public_private_and_host_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    device_id = str(uuid.uuid4())
    credential_id = str(uuid.uuid4())
    public = PublicDeviceManifest(device_id).to_dict()
    private = PrivateDeviceManifest(
        device_id, (HostCredential(credential_id, 2),)
    ).to_dict()
    policy = HostDevicePolicy(
        device_id,
        "Field drive",
        "PUBLIC-UUID",
        "LUKS-UUID",
        True,
        UnlockMode.AUTOMATIC,
        credential_id,
        2,
    )
    save_host_devices(HostDeviceConfig(True, {device_id: policy}))

    assert set(public) == {"device_format_version", "device_id"}
    assert "credentials" not in public
    assert private["credentials"] == {
        "main_keyslot": 0,
        "recovery_keyslot": 1,
        "host_credentials": [{"credential_id": credential_id, "keyslot": 2}],
    }
    assert load_host_devices().devices[device_id] == policy
    host_json = json.dumps(load_host_devices().to_dict())
    assert "passphrase" not in host_json.lower()
    assert "secret" not in host_json.lower()


def test_host_keyslots_are_unique_reserved_and_exhaustible() -> None:
    device_id = str(uuid.uuid4())
    credentials = tuple(HostCredential(str(uuid.uuid4()), slot) for slot in range(2, 32))
    manifest = PrivateDeviceManifest(device_id, credentials)

    with pytest.raises(DeviceError, match="no free LUKS2 keyslot"):
        manifest.next_host_keyslot()
    with pytest.raises(DeviceError, match="between 2 and 31"):
        HostCredential(str(uuid.uuid4()), 1)
    with pytest.raises(DeviceError, match="duplicate"):
        PrivateDeviceManifest(device_id, (credentials[0], credentials[0]))
