from __future__ import annotations

from contextlib import closing

import secretstorage
from secretstorage.exceptions import SecretServiceNotAvailableException

from pandoracle.errors import DeviceError

APPLICATION_ATTRIBUTE = "pandoracle-device"


class DeviceSecretStore:
    def _attributes(self, device_id: str, luks_uuid: str, credential_id: str) -> dict[str, str]:
        return {
            "application": APPLICATION_ATTRIBUTE,
            "device-id": device_id,
            "luks-uuid": luks_uuid,
            "credential-id": credential_id,
        }

    def available(self) -> bool:
        try:
            with closing(secretstorage.dbus_init()) as connection:
                return secretstorage.check_service_availability(connection)
        except (OSError, SecretServiceNotAvailableException):
            return False

    def store(
        self,
        *,
        device_id: str,
        luks_uuid: str,
        credential_id: str,
        secret: bytes,
    ) -> None:
        try:
            with closing(secretstorage.dbus_init()) as connection:
                if not secretstorage.check_service_availability(connection):
                    raise DeviceError("desktop Secret Service is unavailable")
                collection = secretstorage.get_default_collection(connection)
                if collection.is_locked():
                    collection.unlock()
                collection.create_item(
                    f"Pandoracle automatic unlock ({device_id[:8]})",
                    self._attributes(device_id, luks_uuid, credential_id),
                    secret,
                    replace=True,
                )
        except DeviceError:
            raise
        except Exception as error:
            raise DeviceError(f"could not store automatic-unlock credential: {error}") from error

    def lookup(self, *, device_id: str, luks_uuid: str, credential_id: str) -> bytes | None:
        try:
            with closing(secretstorage.dbus_init()) as connection:
                if not secretstorage.check_service_availability(connection):
                    return None
                items = list(
                    secretstorage.search_items(
                        connection, self._attributes(device_id, luks_uuid, credential_id)
                    )
                )
                if not items:
                    return None
                if len(items) != 1:
                    raise DeviceError("automatic-unlock secret is ambiguous")
                if items[0].is_locked():
                    items[0].unlock()
                return items[0].get_secret()
        except DeviceError:
            raise
        except Exception as error:
            raise DeviceError(f"could not read automatic-unlock credential: {error}") from error

    def delete(self, *, device_id: str, luks_uuid: str, credential_id: str) -> bool:
        try:
            with closing(secretstorage.dbus_init()) as connection:
                if not secretstorage.check_service_availability(connection):
                    return False
                items = list(
                    secretstorage.search_items(
                        connection, self._attributes(device_id, luks_uuid, credential_id)
                    )
                )
                for item in items:
                    if item.is_locked():
                        item.unlock()
                    item.delete()
                return bool(items)
        except Exception as error:
            raise DeviceError(f"could not delete automatic-unlock credential: {error}") from error


def wipe_secret(value: bytearray | None) -> None:
    if value is not None:
        value[:] = b"\x00" * len(value)
