from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pandoracle.config import clear_active_device, runtime_directory, set_active_device
from pandoracle.device_credentials import (
    LuksKeyslotManager,
    generate_host_credential,
    generate_recovery_credential,
)
from pandoracle.device_models import (
    WORKSPACE_DIRECTORY,
    HostCredential,
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
from pandoracle.device_privileged import (
    HELPER_PATH,
    PrivilegedDeviceProvisioner,
    PrivilegedLuksKeyslotManager,
    PrivilegedProvisioningTransaction,
    unlock_after_privileged_provisioning,
)
from pandoracle.device_public import (
    PublicContentSummary,
    ensure_public_marker_hidden,
    inspect_public_directory,
    replace_public_contents,
)
from pandoracle.device_secrets import DeviceSecretStore, wipe_secret
from pandoracle.device_udisks import (
    ConnectedDevice,
    DriveCandidate,
    ProvisionedDevice,
    UDisksClient,
    sync_filesystem,
)
from pandoracle.errors import DeviceBusyError, DeviceError
from pandoracle.maintenance import recover
from pandoracle.workspace import Workspace

CredentialProvider = Callable[[str], bytearray]
RecoveryAcknowledgement = Callable[[str], bool]
ProcessFactory = Callable[..., subprocess.Popen[Any]]
CloseRetry = Callable[[str], None]


@dataclass(frozen=True)
class SetupResult:
    device_id: str
    name: str
    workspace_id: str
    automatic_unlock: bool
    automatic_unlock_error: str | None = None


@dataclass(frozen=True)
class ForgetResult:
    name: str
    device_key_removed: bool
    orphaned_key_possible: bool


class DeviceService:
    def __init__(
        self,
        *,
        udisks: UDisksClient | None = None,
        keyslots: LuksKeyslotManager | None = None,
        secrets_store: DeviceSecretStore | None = None,
        process_factory: ProcessFactory = subprocess.Popen,
        privileged_provisioner: PrivilegedDeviceProvisioner | None = None,
    ):
        self.udisks = udisks or UDisksClient()
        if keyslots is not None:
            self.keyslots = keyslots
        elif HELPER_PATH.exists():
            self.keyslots = PrivilegedLuksKeyslotManager()
        else:
            # Source checkouts can manage an already provisioned development
            # device without ever elevating checkout code. Release packages
            # install the fixed helper and take the branch above.
            self.keyslots = LuksKeyslotManager()
        self.secrets = secrets_store or DeviceSecretStore()
        self.process_factory = process_factory
        self.privileged_provisioner = privileged_provisioner
        if privileged_provisioner is None and udisks is None and keyslots is None:
            self.privileged_provisioner = PrivilegedDeviceProvisioner()

    def candidates(self) -> list[DriveCandidate]:
        return self.udisks.list_candidates()

    def probable_devices(self) -> list[ConnectedDevice]:
        return self.udisks.probable_devices()

    def update_policy(self, policy: HostDevicePolicy) -> None:
        self._save_policy(policy)

    def setup(
        self,
        candidate: DriveCandidate,
        *,
        name: str,
        main_passphrase: bytearray,
        acknowledge_recovery: RecoveryAcknowledgement,
        automatic_unlock: bool,
        public_directory: Path | None = None,
    ) -> SetupResult:
        if not main_passphrase:
            raise DeviceError("main passphrase must not be empty")
        if public_directory is not None:
            inspect_public_directory(public_directory)
        recovery_secret = generate_recovery_credential()
        host_secret: bytearray | None = None
        provisioned: ProvisionedDevice | None = None
        private_root: Path | None = None
        device_id: str | None = None
        enrolled: HostCredential | None = None
        automatic_unlock_error: str | None = None
        transaction: PrivilegedProvisioningTransaction | None = None
        provisioner = self.privileged_provisioner
        privileged_keys = provisioner is not None
        try:
            if provisioner is not None:
                if automatic_unlock:
                    host_secret = generate_host_credential()
                    enrolled = HostCredential(str(uuid.uuid4()), 2)
                transaction = provisioner.start(
                    candidate,
                    main_passphrase=main_passphrase,
                    recovery_credential=recovery_secret,
                    host_credential=host_secret,
                )
                provisioned = transaction.provisioned
            else:
                provisioned = self.udisks.provision(candidate, main_passphrase)
                occupied = self.keyslots.occupied_slots(provisioned.encrypted_device)
                if occupied != {0}:
                    raise DeviceError(
                        "new LUKS2 volume did not contain exactly main keyslot 0"
                    )
                self.keyslots.add_key(
                    provisioned.encrypted_device,
                    existing_credential=main_passphrase,
                    new_credential=recovery_secret,
                    keyslot=1,
                )
            recovery_text = bytes(recovery_secret).decode("ascii")
            try:
                recovery_acknowledged = acknowledge_recovery(recovery_text)
            except BaseException:
                if transaction is not None:
                    transaction.abort()
                    transaction = None
                    enrolled = None
                else:
                    self.keyslots.remove_key(
                        provisioned.encrypted_device, recovery_secret, keyslot=1
                    )
                raise
            if not recovery_acknowledged:
                if transaction is not None:
                    transaction.abort()
                    transaction = None
                    enrolled = None
                else:
                    self.keyslots.remove_key(
                        provisioned.encrypted_device, recovery_secret, keyslot=1
                    )
                raise DeviceError("device setup cancelled before recovery credential confirmation")

            device_id = str(uuid.uuid4())
            if transaction is not None:
                outcome = "commit"
                if enrolled is not None and host_secret is not None:
                    try:
                        self.secrets.store(
                            device_id=device_id,
                            luks_uuid=provisioned.luks_uuid,
                            credential_id=enrolled.credential_id,
                            secret=bytes(host_secret),
                        )
                    except DeviceError as error:
                        automatic_unlock_error = str(error)
                        enrolled = None
                        outcome = "prompt"
                transaction.finish(outcome)
                transaction = None
                provisioned = unlock_after_privileged_provisioning(
                    self.udisks, provisioned, main_passphrase
                )
            public_root = self.udisks.mount(provisioned.public_path)
            private_root = self.udisks.mount(provisioned.cleartext_path)
            private_manifest = PrivateDeviceManifest(device_id)
            workspace = Workspace.create(private_root / WORKSPACE_DIRECTORY)

            if automatic_unlock and not privileged_keys:
                try:
                    enrolled, host_secret = self._enroll_host_credential(
                        provisioned.encrypted_device,
                        provisioned.luks_uuid,
                        private_manifest,
                        main_passphrase,
                    )
                    private_manifest = private_manifest.with_host_credential(enrolled)
                except DeviceError as error:
                    automatic_unlock_error = str(error)
            elif enrolled is not None:
                private_manifest = private_manifest.with_host_credential(enrolled)
            write_private_manifest(private_root, private_manifest)
            if public_directory is not None:
                replace_public_contents(public_root, public_directory)
            # The public marker is the final provisioning commit marker.
            write_public_manifest(public_root, PublicDeviceManifest(device_id))
            ensure_public_marker_hidden(public_root)
            sync_filesystem(public_root)
            sync_filesystem(private_root)

            policy = HostDevicePolicy(
                device_id,
                name.strip() or f"Pandora {device_id[:8]}",
                provisioned.public_uuid,
                provisioned.luks_uuid,
                True,
                UnlockMode.AUTOMATIC if enrolled else UnlockMode.PROMPT,
                enrolled.credential_id if enrolled else None,
                enrolled.keyslot if enrolled else None,
            )
            config = load_host_devices()
            config.devices[device_id] = policy
            save_host_devices(config)
            self._safe_close_connected(
                ConnectedDevice(
                    provisioned.drive_path,
                    provisioned.public_path,
                    Path("/dev/null"),
                    provisioned.encrypted_path,
                    provisioned.encrypted_device,
                    provisioned.public_uuid,
                    provisioned.luks_uuid,
                    provisioned.cleartext_path,
                ),
                private_root,
            )
            provisioned = None
            return SetupResult(
                device_id,
                policy.name,
                workspace.manifest.workspace_id,
                bool(enrolled),
                automatic_unlock_error,
            )
        except BaseException:
            if transaction is not None:
                with suppress(Exception):
                    transaction.abort()
            if provisioned is not None and enrolled is not None and host_secret is not None:
                with suppress(Exception):
                    self.keyslots.remove_key(
                        provisioned.encrypted_device,
                        host_secret,
                        keyslot=enrolled.keyslot,
                    )
                if device_id is not None:
                    with suppress(Exception):
                        self.secrets.delete(
                            device_id=device_id,
                            luks_uuid=provisioned.luks_uuid,
                            credential_id=enrolled.credential_id,
                        )
                    if private_root is not None:
                        with suppress(Exception):
                            write_private_manifest(
                                private_root, PrivateDeviceManifest(device_id)
                            )
            if provisioned is not None:
                self._best_effort_close(provisioned)
            raise
        finally:
            wipe_secret(recovery_secret)
            wipe_secret(host_secret)
            wipe_secret(main_passphrase)

    def trust(
        self,
        connected: ConnectedDevice,
        *,
        name: str,
        credential: bytearray,
        automatic_unlock: bool,
    ) -> HostDevicePolicy:
        public_root: Path | None = None
        private_root: Path | None = None
        clear: str | None = None
        public_was_mounted = bool(self.udisks.mount_points(connected.public_path))
        host_secret: bytearray | None = None
        try:
            public_root = self.udisks.mount(connected.public_path)
            public = read_public_manifest(public_root)
            ensure_public_marker_hidden(public_root)
            clear = connected.cleartext_path or self.udisks.unlock(
                connected.encrypted_path, credential
            )
            private_root = self.udisks.mount(clear)
            self._ensure_ownership(clear, private_root / WORKSPACE_DIRECTORY)
            private = read_private_manifest(private_root)
            if private.device_id != public.device_id:
                raise DeviceError("public and private Pandora device identities differ")
            workspace = Workspace.open(private_root / private.workspace)
            recover(workspace)
            original_private = private
            host_credential: HostCredential | None = None
            if automatic_unlock:
                host_credential, host_secret = self._enroll_host_credential(
                    connected.encrypted_device,
                    connected.luks_uuid,
                    private,
                    credential,
                )
                private = private.with_host_credential(host_credential)
            policy = HostDevicePolicy(
                public.device_id,
                name.strip() or f"Pandora {public.device_id[:8]}",
                connected.public_uuid,
                connected.luks_uuid,
                True,
                UnlockMode.AUTOMATIC if host_credential else UnlockMode.PROMPT,
                host_credential.credential_id if host_credential else None,
                host_credential.keyslot if host_credential else None,
            )
            config = load_host_devices()
            config.devices[policy.device_id] = policy
            try:
                if host_credential is not None:
                    write_private_manifest(private_root, private)
                save_host_devices(config)
            except BaseException:
                if host_credential is not None and host_secret is not None:
                    with suppress(Exception):
                        self.secrets.delete(
                            device_id=policy.device_id,
                            luks_uuid=policy.luks_uuid,
                            credential_id=host_credential.credential_id,
                        )
                    with suppress(Exception):
                        self.keyslots.remove_key(
                            connected.encrypted_device,
                            host_secret,
                            keyslot=host_credential.keyslot,
                        )
                    with suppress(Exception):
                        write_private_manifest(private_root, original_private)
                raise
            return policy
        finally:
            try:
                if private_root is not None:
                    sync_filesystem(private_root)
                    if clear is not None:
                        self.udisks.unmount(clear)
                if self.udisks.cleartext_path(connected.encrypted_path):
                    self.udisks.lock(connected.encrypted_path)
                if public_root is not None and not public_was_mounted:
                    self.udisks.unmount(connected.public_path)
            finally:
                wipe_secret(host_secret)
                wipe_secret(credential)

    def enable_automatic(self, policy: HostDevicePolicy, credential: bytearray) -> HostDevicePolicy:
        if policy.unlock_mode is UnlockMode.AUTOMATIC:
            wipe_secret(credential)
            return policy
        connected: ConnectedDevice | None = None
        clear: str | None = None
        clear_was_open = False
        private_was_mounted = False
        try:
            connected = self._require_connected(policy)
            clear_was_open = connected.cleartext_path is not None
            clear = connected.cleartext_path or self.udisks.unlock(
                connected.encrypted_path, credential
            )
            private_was_mounted = bool(self.udisks.mount_points(clear))
            private_root = self.udisks.mount(clear)
        except BaseException:
            if connected is not None and clear is not None and not clear_was_open:
                with suppress(DeviceError):
                    self.udisks.lock(connected.encrypted_path)
            wipe_secret(credential)
            raise
        host_secret: bytearray | None = None
        enrolled: HostCredential | None = None
        try:
            private = self._validated_private(private_root, policy.device_id)
            enrolled, host_secret = self._enroll_host_credential(
                connected.encrypted_device,
                connected.luks_uuid,
                private,
                credential,
            )
            updated_private = private.with_host_credential(enrolled)
            try:
                write_private_manifest(private_root, updated_private)
                updated = replace(
                    policy,
                    unlock_mode=UnlockMode.AUTOMATIC,
                    credential_id=enrolled.credential_id,
                    credential_keyslot=enrolled.keyslot,
                )
                self._save_policy(updated)
            except BaseException:
                self.secrets.delete(
                    device_id=policy.device_id,
                    luks_uuid=policy.luks_uuid,
                    credential_id=enrolled.credential_id,
                )
                self.keyslots.remove_key(
                    connected.encrypted_device,
                    host_secret,
                    keyslot=enrolled.keyslot,
                )
                write_private_manifest(private_root, private)
                raise
            return updated
        finally:
            try:
                sync_filesystem(private_root)
                if not private_was_mounted:
                    self.udisks.unmount(clear)
                if not clear_was_open:
                    self.udisks.lock(connected.encrypted_path)
            finally:
                wipe_secret(host_secret)
                wipe_secret(credential)

    def disable_automatic(self, policy: HostDevicePolicy) -> HostDevicePolicy:
        if policy.unlock_mode is UnlockMode.PROMPT:
            return policy
        if policy.credential_id is None or policy.credential_keyslot is None:
            raise DeviceError("automatic-unlock policy is missing its host credential")
        secret = self.secrets.lookup(
            device_id=policy.device_id,
            luks_uuid=policy.luks_uuid,
            credential_id=policy.credential_id,
        )
        if secret is None:
            raise DeviceError("automatic-unlock secret is unavailable; use device forget")
        mutable = bytearray(secret)
        connected: ConnectedDevice | None = None
        clear: str | None = None
        clear_was_open = False
        private_was_mounted = False
        try:
            connected = self._require_connected(policy)
            clear_was_open = connected.cleartext_path is not None
            clear = connected.cleartext_path or self.udisks.unlock(
                connected.encrypted_path, mutable
            )
            private_was_mounted = bool(self.udisks.mount_points(clear))
            private_root = self.udisks.mount(clear)
        except BaseException:
            if connected is not None and clear is not None and not clear_was_open:
                with suppress(DeviceError):
                    self.udisks.lock(connected.encrypted_path)
            wipe_secret(mutable)
            raise
        try:
            private = self._validated_private(private_root, policy.device_id)
            self.keyslots.remove_key(
                connected.encrypted_device,
                mutable,
                keyslot=policy.credential_keyslot,
            )
            write_private_manifest(
                private_root, private.without_host_credential(policy.credential_id)
            )
            self.secrets.delete(
                device_id=policy.device_id,
                luks_uuid=policy.luks_uuid,
                credential_id=policy.credential_id,
            )
            updated = replace(
                policy,
                unlock_mode=UnlockMode.PROMPT,
                credential_id=None,
                credential_keyslot=None,
            )
            self._save_policy(updated)
            return updated
        finally:
            try:
                sync_filesystem(private_root)
                if not private_was_mounted:
                    self.udisks.unmount(clear)
                if not clear_was_open:
                    self.udisks.lock(connected.encrypted_path)
            finally:
                wipe_secret(mutable)

    def forget(self, policy: HostDevicePolicy) -> ForgetResult:
        connected = self.udisks.connected_for_policy(policy)
        removed = False
        orphaned = policy.unlock_mode is UnlockMode.AUTOMATIC and connected is None
        secret: bytearray | None = None
        error: Exception | None = None
        try:
            if policy.credential_id is not None:
                value = self.secrets.lookup(
                    device_id=policy.device_id,
                    luks_uuid=policy.luks_uuid,
                    credential_id=policy.credential_id,
                )
                secret = None if value is None else bytearray(value)
            if connected is not None and secret is not None and policy.credential_id is not None:
                clear = connected.cleartext_path or self.udisks.unlock(
                    connected.encrypted_path, secret
                )
                private_root = self.udisks.mount(clear)
                try:
                    private = self._validated_private(private_root, policy.device_id)
                    if policy.credential_keyslot is None:
                        raise DeviceError(
                            "automatic-unlock policy is missing its host credential"
                        )
                    self.keyslots.remove_key(
                        connected.encrypted_device,
                        secret,
                        keyslot=policy.credential_keyslot,
                    )
                    write_private_manifest(
                        private_root, private.without_host_credential(policy.credential_id)
                    )
                    removed = True
                finally:
                    sync_filesystem(private_root)
                    self.udisks.unmount(clear)
                    self.udisks.lock(connected.encrypted_path)
            elif policy.unlock_mode is UnlockMode.AUTOMATIC:
                orphaned = True
        except Exception as caught:
            orphaned = policy.unlock_mode is UnlockMode.AUTOMATIC
            error = caught
        finally:
            try:
                if policy.credential_id is not None:
                    try:
                        self.secrets.delete(
                            device_id=policy.device_id,
                            luks_uuid=policy.luks_uuid,
                            credential_id=policy.credential_id,
                        )
                    except Exception as caught:
                        error = error or caught
                try:
                    config = load_host_devices()
                    config.devices.pop(policy.device_id, None)
                    save_host_devices(config)
                except Exception as caught:
                    error = error or caught
            finally:
                wipe_secret(secret)
        if error is not None:
            raise DeviceError(
                f"host trust was removed, but device-key cleanup failed: {error}"
            ) from error
        return ForgetResult(policy.name, removed, orphaned)

    def open_session(
        self,
        policy: HostDevicePolicy,
        credential_provider: CredentialProvider,
        close_retry: CloseRetry | None = None,
    ) -> int:
        session_lock = _acquire_device_session_lock(policy.device_id)
        try:
            return self._open_session_locked(policy, credential_provider, close_retry)
        finally:
            os.close(session_lock)

    def _open_session_locked(
        self,
        policy: HostDevicePolicy,
        credential_provider: CredentialProvider,
        close_retry: CloseRetry | None,
    ) -> int:
        connected = self._require_connected(policy)
        public_mounts = self.udisks.mount_points(connected.public_path)
        if public_mounts:
            ensure_public_marker_hidden(public_mounts[0])
        clear = connected.cleartext_path
        automatic_secret: bytearray | None = None
        if clear is None and policy.unlock_mode is UnlockMode.AUTOMATIC:
            if policy.credential_id is None:
                raise DeviceError("automatic-unlock policy is missing its host credential")
            value = self.secrets.lookup(
                device_id=policy.device_id,
                luks_uuid=policy.luks_uuid,
                credential_id=policy.credential_id,
            )
            if value is not None:
                automatic_secret = bytearray(value)
                try:
                    clear = self.udisks.unlock(connected.encrypted_path, automatic_secret)
                except DeviceError:
                    clear = None
                finally:
                    wipe_secret(automatic_secret)
        if clear is None:
            manual = credential_provider(f"Unlock {policy.name}")
            try:
                clear = self.udisks.unlock(connected.encrypted_path, manual)
            finally:
                wipe_secret(manual)
        pid = os.getpid()
        import threading

        removed = threading.Event()
        closing = threading.Event()
        stopped = threading.Event()
        monitor = threading.Thread(
            target=self.udisks.monitor_removal,
            args=(
                {connected.drive_path, connected.encrypted_path, clear},
                connected.drive_path,
                closing,
                removed,
                stopped,
            ),
            daemon=True,
        )
        monitor_started = False
        physically_removed = False
        private_root: Path | None = None
        child: subprocess.Popen[Any] | None = None
        terminal_fd: int | None = None
        result = 1
        try:
            monitor.start()
            monitor_started = True
            private_root = self.udisks.mount(clear)
            if removed.is_set():
                raise DeviceError("Pandora device was removed while opening")
            self._ensure_ownership(clear, private_root / WORKSPACE_DIRECTORY)
            private = self._validated_private(private_root, policy.device_id)
            workspace = Workspace.open(private_root / private.workspace)
            recover(workspace)
            if removed.is_set():
                raise DeviceError("Pandora device was removed while opening")
            set_active_device(device_id=policy.device_id, workspace=workspace.root, pid=pid)
            child = self.process_factory(
                [
                    sys.executable,
                    "-m",
                    "pandoracle",
                    "shell",
                    "--workspace",
                    str(workspace.root),
                ],
                process_group=0,
            )
            terminal_fd = _give_terminal_to(child.pid)
            while child.poll() is None:
                if removed.wait(0.1):
                    physically_removed = True
                    _terminate_process_group(child)
                    break
            result = int(child.wait())
        finally:
            clear_active_device(pid=pid)
            # Mapping disappearance is an error while the shell is active, but
            # becomes an expected effect of Lock() during clean close. Continue
            # monitoring the physical drive throughout the close/retry phase.
            closing.set()
            physically_removed = (
                physically_removed
                or removed.is_set()
                or not self._drive_is_present(connected.drive_path)
            )
            if child is not None and child.poll() is None:
                _terminate_process_group(child)
            _restore_terminal(terminal_fd)
            try:
                if not physically_removed:
                    reported: str | None = None
                    while not removed.is_set():
                        try:
                            self._safe_close_connected(
                                connected, private_root, cleartext_path=clear
                            )
                            break
                        except DeviceBusyError as error:
                            if close_retry is None:
                                raise DeviceError(
                                    "device is still in use; close programs using it and run "
                                    "'pandoracle device close'"
                                ) from error
                            message = str(error)
                            if message != reported:
                                close_retry(message)
                                reported = message
                            # Keep the physical-device monitor alive while waiting.
                            # This replaces a blocking prompt that could not notice
                            # removal and left its dedicated terminal stranded.
                            removed.wait(1.0)
                        except DeviceError:
                            if removed.is_set() or not self._drive_is_present(
                                connected.drive_path
                            ):
                                physically_removed = True
                                break
                            raise
                    physically_removed = physically_removed or removed.is_set()
            finally:
                stopped.set()
                if monitor_started:
                    monitor.join(timeout=1)
        return result

    def close(self, policy: HostDevicePolicy) -> None:
        connected = self._require_connected(policy)
        clear = connected.cleartext_path or self.udisks.cleartext_path(connected.encrypted_path)
        if clear is None:
            if self.udisks.mount_points(connected.public_path):
                self.udisks.unmount(connected.public_path)
            with suppress(DeviceError):
                self.udisks.power_off(connected.drive_path)
            return
        mounts = self.udisks.mount_points(clear)
        self._safe_close_connected(connected, mounts[0] if mounts else None, cleartext_path=clear)

    def replace_public_contents(
        self, policy: HostDevicePolicy, source: Path
    ) -> PublicContentSummary:
        inspect_public_directory(source)
        connected = self._require_connected(policy)
        existing = self.udisks.mount_points(connected.public_path)
        root = existing[0] if existing else self.udisks.mount(connected.public_path)
        try:
            public = read_public_manifest(root)
            if public.device_id != policy.device_id:
                raise DeviceError("public device identity differs from host policy")
            ensure_public_marker_hidden(root)
            result = replace_public_contents(root, source)
            sync_filesystem(root)
            return result
        finally:
            if not existing:
                self.udisks.unmount(connected.public_path)

    def _enroll_host_credential(
        self,
        encrypted_device: Path,
        luks_uuid: str,
        private: PrivateDeviceManifest,
        existing_credential: bytearray,
    ) -> tuple[HostCredential, bytearray]:
        recorded = {item.keyslot for item in private.host_credentials}
        slot = next(
            (
                candidate
                for candidate in range(2, 32)
                if candidate not in recorded
            ),
            None,
        )
        if slot is None:
            raise DeviceError(
                "automatic unlock has no free LUKS2 keyslot; forget an obsolete trusted host"
            )
        credential = HostCredential(str(uuid.uuid4()), slot)
        secret = generate_host_credential()
        try:
            self.keyslots.add_key(
                encrypted_device,
                existing_credential=existing_credential,
                new_credential=secret,
                keyslot=slot,
            )
            self.secrets.store(
                device_id=private.device_id,
                luks_uuid=luks_uuid,
                credential_id=credential.credential_id,
                secret=bytes(secret),
            )
        except BaseException as error:
            cleanup_error: Exception | None = None
            try:
                self.keyslots.remove_key(encrypted_device, secret, keyslot=slot)
            except Exception as caught:
                cleanup_error = caught
            wipe_secret(secret)
            if cleanup_error is not None:
                raise DeviceError(
                    "automatic unlock failed and its new device keyslot could not be "
                    f"removed: {cleanup_error}"
                ) from error
            raise
        return credential, secret

    def _require_connected(self, policy: HostDevicePolicy) -> ConnectedDevice:
        connected = self.udisks.connected_for_policy(policy)
        if connected is None:
            raise DeviceError(f"Pandora device is not connected: {policy.name}")
        return connected

    def _validated_private(self, root: Path, device_id: str) -> PrivateDeviceManifest:
        value = read_private_manifest(root)
        if value.device_id != device_id:
            raise DeviceError("private device identity differs from host policy")
        return value

    def _ensure_ownership(self, cleartext_path: str, workspace: Path) -> None:
        if workspace.exists() and workspace.stat().st_uid != os.getuid():
            self.udisks.take_ownership(cleartext_path)

    def _drive_is_present(self, drive_path: str) -> bool:
        try:
            return self.udisks.is_present({drive_path})
        except DeviceError:
            # The event monitor independently treats a lost UDisks connection as
            # removal. Do not turn a transient status check into a false removal.
            return True

    def _save_policy(self, policy: HostDevicePolicy) -> None:
        config = load_host_devices()
        config.devices[policy.device_id] = policy
        save_host_devices(config)

    def _safe_close_connected(
        self,
        connected: ConnectedDevice,
        private_root: Path | None,
        *,
        cleartext_path: str | None = None,
    ) -> None:
        del private_root, cleartext_path
        clear = self.udisks.cleartext_path(connected.encrypted_path)
        if clear is not None:
            private_mounts = self.udisks.mount_points(clear)
            if private_mounts:
                sync_filesystem(private_mounts[0])
                try:
                    self.udisks.unmount(clear)
                except DeviceBusyError as error:
                    raise _close_busy_error("private workspace", private_mounts[0]) from error
            self.udisks.lock(connected.encrypted_path)
        public_mounts = self.udisks.mount_points(connected.public_path)
        if public_mounts:
            try:
                self.udisks.unmount(connected.public_path)
            except DeviceBusyError as error:
                raise _close_busy_error("public device area", public_mounts[0]) from error
        with suppress(DeviceError):
            self.udisks.power_off(connected.drive_path)

    def _best_effort_close(self, value: ProvisionedDevice) -> None:
        try:
            mounts = self.udisks.mount_points(value.cleartext_path)
            if mounts:
                sync_filesystem(mounts[0])
                self.udisks.unmount(value.cleartext_path)
            if self.udisks.cleartext_path(value.encrypted_path):
                self.udisks.lock(value.encrypted_path)
        except Exception:
            pass


def _terminate_process_group(child: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=1)
    except ProcessLookupError:
        pass


def _close_busy_error(area: str, root: Path) -> DeviceBusyError:
    holders = _mount_holders(root)
    if not holders:
        return DeviceBusyError(f"{area} is still in use by another process")
    return DeviceBusyError(f"{area} is still in use by {', '.join(holders)}")


def _mount_holders(root: Path) -> list[str]:
    """Describe same-user processes with a cwd or open descriptor below root."""

    try:
        mount_root = root.resolve(strict=True)
        processes = list(Path("/proc").iterdir())
    except OSError:
        return []
    holders: list[str] = []
    current_uid = os.getuid()
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != current_uid:
                continue
            targets = [process / "cwd", *(process / "fd").iterdir()]
            uses_mount = any(
                Path(os.readlink(target)).is_relative_to(mount_root) for target in targets
            )
            if not uses_mount:
                continue
            name = (process / "comm").read_text(encoding="utf-8").strip()
            holders.append(f"{name or 'process'} (PID {process.name})")
        except OSError:
            continue
    return sorted(set(holders))


def _acquire_device_session_lock(device_id: str) -> int:
    path = runtime_directory() / f"device-session-{device_id}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise DeviceError("this Pandora device already has an opening session") from error
    return descriptor


def _give_terminal_to(process_group: int) -> int | None:
    if not sys.stdin.isatty():
        return None
    try:
        descriptor = sys.stdin.fileno()
        _set_terminal_process_group(descriptor, process_group)
        return descriptor
    except (OSError, ValueError):
        return None


def _restore_terminal(descriptor: int | None) -> None:
    if descriptor is not None:
        with suppress(OSError):
            _set_terminal_process_group(descriptor, os.getpgrp())


def _set_terminal_process_group(descriptor: int, process_group: int) -> None:
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(descriptor, process_group)
    finally:
        signal.signal(signal.SIGTTOU, previous)
