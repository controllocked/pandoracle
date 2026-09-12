from __future__ import annotations

import base64
import io
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO

from pandoracle.device_credentials import LuksKeyslotManager, _validate_system_executable
from pandoracle.device_secrets import wipe_secret
from pandoracle.device_udisks import (
    DriveCandidate,
    ProvisionedDevice,
    UDisksClient,
    sync_filesystem,
)
from pandoracle.errors import DeviceError

HELPER_PATH = Path("/usr/libexec/pandoracle-device-provision")
PROTOCOL_VERSION = 1


@dataclass
class PrivilegedProvisioningTransaction:
    process: subprocess.Popen[bytes]
    provisioned: ProvisionedDevice

    def finish(self, outcome: str) -> None:
        if outcome not in {"commit", "prompt", "abort"}:
            raise ValueError("invalid privileged provisioning outcome")
        if self.process.stdin is None or self.process.stdout is None:
            raise DeviceError("privileged helper communication is unavailable")
        try:
            self.process.stdin.write(outcome.encode("ascii") + b"\n")
            self.process.stdin.flush()
            reply = _read_reply(self.process.stdout)
            returncode = self.process.wait(timeout=300)
        except BaseException:
            self.process.kill()
            self.process.wait()
            raise
        finally:
            self.process.stdin.close()
        if returncode != 0 or reply.get("status") != "done":
            raise DeviceError(_helper_error(reply, self.process))

    def abort(self) -> None:
        if self.process.poll() is None:
            self.finish("abort")


class PrivilegedDeviceProvisioner:
    def __init__(
        self,
        *,
        helper_path: Path = HELPER_PATH,
        pkexec_path: Path | None = None,
        process_factory: Any = subprocess.Popen,
    ):
        self.helper_path = helper_path
        self.pkexec_path = pkexec_path
        self.process_factory = process_factory

    def start(
        self,
        candidate: DriveCandidate,
        *,
        main_passphrase: bytes | bytearray,
        recovery_credential: bytes | bytearray,
        host_credential: bytes | bytearray | None,
    ) -> PrivilegedProvisioningTransaction:
        pkexec = self.pkexec_path or _system_helper_executable("pkexec")
        try:
            _validate_system_executable(pkexec)
            _validate_root_owned_chain(self.helper_path)
        except DeviceError as error:
            raise DeviceError(
                "the root-owned Pandora device helper is not safely installed; "
                "install the system package before running device setup"
            ) from error
        request = {
            "version": PROTOCOL_VERSION,
            "action": "provision",
            "device": str(candidate.device),
            "drive_path": candidate.object_path,
            "block_path": candidate.block_path,
            "serial": candidate.serial,
            "size": candidate.size,
            "uid": os.getuid(),
            "main": _encode_secret(main_passphrase),
            "recovery": _encode_secret(recovery_credential),
            "host": None if host_credential is None else _encode_secret(host_credential),
        }
        process = self.process_factory(
            [str(pkexec), str(self.helper_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None:
            process.kill()
            process.wait()
            raise DeviceError("privileged helper communication is unavailable")
        try:
            process.stdin.write(_encode_request(request))
            process.stdin.flush()
            reply = _read_reply(process.stdout)
            if reply.get("status") != "ready":
                process.stdin.close()
                process.wait(timeout=300)
                raise DeviceError(_helper_error(reply, process))
            provisioned = ProvisionedDevice(
                str(reply["drive_path"]),
                str(reply["public_path"]),
                str(reply["encrypted_path"]),
                "",
                str(reply["public_uuid"]),
                str(reply["luks_uuid"]),
                Path(str(reply["encrypted_device"])),
            )
            return PrivilegedProvisioningTransaction(process, provisioned)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise


class PrivilegedLuksKeyslotManager:
    def __init__(
        self,
        *,
        helper_path: Path = HELPER_PATH,
        pkexec_path: Path | None = None,
        runner: Any = subprocess.run,
    ):
        self.helper_path = helper_path
        self.pkexec_path = pkexec_path
        self.runner = runner

    def add_key(
        self,
        device: Path,
        *,
        existing_credential: bytes | bytearray,
        new_credential: bytes | bytearray,
        keyslot: int,
    ) -> None:
        self._run(
            {
                "version": PROTOCOL_VERSION,
                "action": "add-key",
                "device": str(device),
                "keyslot": keyslot,
                "existing": _encode_secret(existing_credential),
                "new": _encode_secret(new_credential),
            }
        )

    def remove_key(
        self, device: Path, credential: bytes | bytearray, *, keyslot: int
    ) -> None:
        self._run(
            {
                "version": PROTOCOL_VERSION,
                "action": "remove-key",
                "device": str(device),
                "keyslot": keyslot,
                "existing": _encode_secret(credential),
            }
        )

    def occupied_slots(self, _device: Path) -> set[int]:
        raise DeviceError(
            "keyslot inspection is internal to the privileged Pandora device helper"
        )

    def _run(self, request: dict[str, Any]) -> None:
        pkexec = self.pkexec_path or _system_helper_executable("pkexec")
        try:
            _validate_system_executable(pkexec)
            _validate_root_owned_chain(self.helper_path)
        except DeviceError as error:
            raise DeviceError(
                "the root-owned Pandora device helper is not safely installed; "
                "install the system package before changing automatic unlock"
            ) from error
        result = self.runner(
            [str(pkexec), str(self.helper_path)],
            input=_encode_request(request),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        try:
            reply = _read_reply(io.BytesIO(result.stdout))
        except DeviceError:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise DeviceError(detail or "privileged LUKS keyslot operation failed") from None
        if result.returncode != 0 or reply.get("status") != "done":
            message = reply.get("message")
            raise DeviceError(
                str(message) if message else "privileged LUKS keyslot operation failed"
            )


def privileged_helper_main() -> None:
    main = bytearray()
    recovery = bytearray()
    host: bytearray | None = None
    provisioned: ProvisionedDevice | None = None
    keyslots: LuksKeyslotManager | None = None
    try:
        caller_uid = _validate_privileged_process()
        request = _read_request(sys.stdin.buffer)
        if request.get("action") in {"add-key", "remove-key"}:
            _run_keyslot_request(request)
            _write_reply({"status": "done"})
            return
        candidate = _validated_candidate(request, caller_uid)
        main = _decode_secret(request, "main")
        recovery = _decode_secret(request, "recovery")
        host = (
            None
            if request.get("host") is None
            else _decode_secret(request, "host")
        )
        udisks = UDisksClient()
        keyslots = LuksKeyslotManager(elevate=False)
        provisioned = udisks.provision(candidate, main)
        if keyslots.occupied_slots(provisioned.encrypted_device) != {0}:
            raise DeviceError("new LUKS2 volume did not contain exactly main keyslot 0")
        keyslots.add_key(
            provisioned.encrypted_device,
            existing_credential=main,
            new_credential=recovery,
            keyslot=1,
        )
        if host is not None:
            keyslots.add_key(
                provisioned.encrypted_device,
                existing_credential=main,
                new_credential=host,
                keyslot=2,
            )
        expected_slots = {0, 1, 2} if host is not None else {0, 1}
        if keyslots.occupied_slots(provisioned.encrypted_device) != expected_slots:
            raise DeviceError("new LUKS2 volume has an unexpected keyslot layout")
        private_root = udisks.mount(provisioned.cleartext_path)
        try:
            account = pwd.getpwuid(caller_uid)
            os.chown(private_root, caller_uid, account.pw_gid)
            sync_filesystem(private_root)
        finally:
            udisks.unmount(provisioned.cleartext_path)
            udisks.lock(provisioned.encrypted_path)
            udisks.wait_until_locked(provisioned.encrypted_path)
        _write_reply(
            {
                "status": "ready",
                "drive_path": provisioned.drive_path,
                "public_path": provisioned.public_path,
                "encrypted_path": provisioned.encrypted_path,
                "public_uuid": provisioned.public_uuid,
                "luks_uuid": provisioned.luks_uuid,
                "encrypted_device": str(provisioned.encrypted_device),
            }
        )
        outcome = sys.stdin.buffer.readline(32).strip().decode("ascii", "strict")
        if outcome not in {"commit", "prompt", "abort"}:
            outcome = "abort"
        if outcome in {"prompt", "abort"} and host is not None:
            keyslots.remove_key(provisioned.encrypted_device, host, keyslot=2)
        if outcome == "abort":
            keyslots.remove_key(provisioned.encrypted_device, recovery, keyslot=1)
        _write_reply({"status": "done"})
    except BaseException as error:
        if provisioned is not None and keyslots is not None:
            if host is not None:
                with suppress(Exception):
                    keyslots.remove_key(provisioned.encrypted_device, host, keyslot=2)
            if recovery:
                with suppress(Exception):
                    keyslots.remove_key(
                        provisioned.encrypted_device, recovery, keyslot=1
                    )
        _write_reply({"status": "error", "message": str(error)})
        raise SystemExit(1) from None
    finally:
        wipe_secret(main)
        wipe_secret(recovery)
        wipe_secret(host)


def unlock_after_privileged_provisioning(
    udisks: UDisksClient,
    provisioned: ProvisionedDevice,
    main_passphrase: bytes | bytearray,
) -> ProvisionedDevice:
    clear = udisks.unlock(provisioned.encrypted_path, main_passphrase)
    return replace(provisioned, cleartext_path=clear)


def _validated_candidate(request: dict[str, Any], caller_uid: int) -> DriveCandidate:
    if request.get("version") != PROTOCOL_VERSION or request.get("action") != "provision":
        raise DeviceError("unsupported privileged helper request")
    if request.get("uid") != caller_uid:
        raise DeviceError("privileged helper caller identity differs from the request")
    requested = (
        str(request.get("device", "")),
        str(request.get("drive_path", "")),
        str(request.get("block_path", "")),
        str(request.get("serial", "")),
        request.get("size"),
    )
    for candidate in UDisksClient().list_candidates():
        actual = (
            str(candidate.device),
            candidate.object_path,
            candidate.block_path,
            candidate.serial,
            candidate.size,
        )
        if actual == requested:
            _refuse_live_root_device(candidate.device)
            return candidate
    raise DeviceError("selected removable drive changed before privileged provisioning")


def _refuse_live_root_device(device: Path) -> None:
    try:
        root_number = os.stat("/").st_dev
        root_node = Path(
            f"/sys/dev/block/{os.major(root_number)}:{os.minor(root_number)}"
        ).resolve(strict=True)
        candidate_node = (Path("/sys/class/block") / device.name).resolve(strict=True)
    except OSError as error:
        raise DeviceError(
            "cannot verify that the selected drive is outside the root chain"
        ) from error

    pending = [root_node]
    visited: set[Path] = set()
    while pending:
        node = pending.pop()
        if node in visited:
            continue
        visited.add(node)
        if candidate_node == node or candidate_node in node.parents:
            raise DeviceError("refusing to provision a drive in the live root-device chain")
        slaves = Path("/sys/class/block") / node.name / "slaves"
        if slaves.is_dir():
            pending.extend(item.resolve(strict=True) for item in slaves.iterdir())


def _run_keyslot_request(request: dict[str, Any]) -> None:
    if request.get("version") != PROTOCOL_VERSION:
        raise DeviceError("unsupported privileged helper request")
    action = request.get("action")
    slot = request.get("keyslot")
    if action not in {"add-key", "remove-key"} or not isinstance(slot, int):
        raise DeviceError("invalid privileged keyslot request")
    if not 2 <= slot <= 31:
        raise DeviceError("privileged helper refuses a reserved LUKS2 keyslot")
    device = _validated_luks_device(request.get("device"))
    existing = _decode_secret(request, "existing")
    new: bytearray | None = None
    try:
        keyslots = LuksKeyslotManager(elevate=False)
        if action == "add-key":
            new = _decode_secret(request, "new")
            keyslots.add_key(
                device,
                existing_credential=existing,
                new_credential=new,
                keyslot=slot,
            )
        else:
            keyslots.remove_key(device, existing, keyslot=slot)
    finally:
        wipe_secret(existing)
        wipe_secret(new)


def _validated_luks_device(value: Any) -> Path:
    if not isinstance(value, str):
        raise DeviceError("invalid privileged keyslot device")
    requested = Path(value)
    try:
        resolved = requested.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise DeviceError("encrypted Pandora device is unavailable") from error
    if not stat.S_ISBLK(metadata.st_mode) or resolved.parent != Path("/dev"):
        raise DeviceError("privileged helper refuses a non-block LUKS target")
    for connected in UDisksClient().probable_devices():
        if connected.encrypted_device.resolve() == resolved:
            return resolved
    raise DeviceError("privileged helper refuses an unrecognized Pandora device")


def _validate_privileged_process() -> int:
    if os.geteuid() != 0:
        raise DeviceError("Pandora device helper must run through pkexec")
    value = os.environ.get("PKEXEC_UID")
    if value is None or not value.isdecimal() or int(value) == 0:
        raise DeviceError("Pandora device helper has no valid desktop caller")
    _validate_root_owned_chain(Path(sys.argv[0]))
    _validate_root_owned_chain(Path(__file__))
    return int(value)


def _validate_root_owned_chain(path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DeviceError(f"cannot validate privileged helper path: {path}") from error
    for component in (resolved, *resolved.parents):
        try:
            metadata = component.stat()
        except OSError as error:
            raise DeviceError(
                f"cannot validate privileged helper path: {component}"
            ) from error
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise DeviceError(f"unsafe privileged helper path component: {component}")
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise DeviceError(f"privileged helper is not a regular file: {resolved}")


def _system_helper_executable(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        raise DeviceError(f"required system command is unavailable: {name}")
    return Path(value).resolve()


def _encode_secret(value: bytes | bytearray) -> str:
    return base64.b64encode(bytes(value)).decode("ascii")


def _decode_secret(request: dict[str, Any], name: str) -> bytearray:
    value = request.get(name)
    if not isinstance(value, str):
        raise DeviceError("invalid privileged helper credential payload")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise DeviceError("invalid privileged helper credential payload") from error
    if not decoded or len(decoded) > 4096:
        raise DeviceError("invalid privileged helper credential length")
    return bytearray(decoded)


def _encode_request(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n"


def _read_request(stream: BinaryIO) -> dict[str, Any]:
    raw = stream.readline(32 * 1024)
    if not raw.endswith(b"\n"):
        raise DeviceError("invalid privileged helper request framing")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeviceError("invalid privileged helper request") from error
    if not isinstance(value, dict):
        raise DeviceError("invalid privileged helper request")
    return value


def _write_reply(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write(_encode_request(value))
    sys.stdout.buffer.flush()


def _read_reply(stream: BinaryIO) -> dict[str, Any]:
    return _read_request(stream)


def _helper_error(reply: dict[str, Any], process: subprocess.Popen[bytes]) -> str:
    message = reply.get("message")
    if isinstance(message, str) and message:
        return message
    if process.stderr is None:
        return "privileged Pandora device provisioning failed"
    detail = process.stderr.read(4096).decode("utf-8", "replace").strip()
    return detail or "privileged Pandora device provisioning failed"
