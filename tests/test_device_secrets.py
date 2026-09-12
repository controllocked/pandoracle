from __future__ import annotations

import uuid
from typing import Any

import pytest

from pandoracle import device_secrets
from pandoracle.device_secrets import DeviceSecretStore


def test_secret_service_item_contains_only_host_key_and_lookup_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items: list[FakeItem] = []

    class Connection:
        def close(self) -> None:
            return None

    class Collection:
        def is_locked(self) -> bool:
            return False

        def create_item(
            self,
            label: str,
            attributes: dict[str, str],
            secret: bytes,
            *,
            replace: bool,
        ) -> None:
            assert replace
            items.append(FakeItem(label, attributes, secret))

    class FakeItem:
        def __init__(self, label: str, attributes: dict[str, str], secret: bytes):
            self.label = label
            self.attributes = attributes
            self.secret = secret
            self.deleted = False

        def is_locked(self) -> bool:
            return False

        def get_secret(self) -> bytes:
            return self.secret

        def delete(self) -> None:
            self.deleted = True

    monkeypatch.setattr(device_secrets.secretstorage, "dbus_init", Connection)
    monkeypatch.setattr(
        device_secrets.secretstorage,
        "check_service_availability",
        lambda _connection: True,
    )
    monkeypatch.setattr(
        device_secrets.secretstorage,
        "get_default_collection",
        lambda _connection: Collection(),
    )

    def search(_connection: Any, attributes: dict[str, str]) -> list[FakeItem]:
        return [item for item in items if item.attributes == attributes and not item.deleted]

    monkeypatch.setattr(device_secrets.secretstorage, "search_items", search)
    store = DeviceSecretStore()
    device_id = str(uuid.uuid4())
    credential_id = str(uuid.uuid4())
    host_key = b"random-host-key"

    store.store(
        device_id=device_id,
        luks_uuid="LUKS-UUID",
        credential_id=credential_id,
        secret=host_key,
    )

    assert items[0].secret == host_key
    assert items[0].attributes == {
        "application": "pandoracle-device",
        "device-id": device_id,
        "luks-uuid": "LUKS-UUID",
        "credential-id": credential_id,
    }
    assert "passphrase" not in str(items[0].attributes).lower()
    assert store.lookup(
        device_id=device_id,
        luks_uuid="LUKS-UUID",
        credential_id=credential_id,
    ) == host_key
    assert store.delete(
        device_id=device_id,
        luks_uuid="LUKS-UUID",
        credential_id=credential_id,
    )
    assert items[0].deleted
