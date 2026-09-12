from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from pandoracle.config import runtime_directory
from pandoracle.errors import DeviceError

CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


def generate_recovery_credential() -> bytearray:
    encoded = base64.b32encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    grouped = "-".join(encoded[index : index + 4] for index in range(0, len(encoded), 4))
    return bytearray(grouped, "ascii")


def generate_host_credential() -> bytearray:
    return bytearray(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"="))


class LuksKeyslotManager:
    def __init__(
        self,
        *,
        runner: CommandRunner = subprocess.run,
        pkexec_path: Path | None = None,
        cryptsetup_path: Path | None = None,
        elevate: bool = True,
    ):
        self.runner = runner
        self.pkexec_path = pkexec_path or _system_executable("pkexec")
        self.cryptsetup_path = cryptsetup_path or _system_executable("cryptsetup")
        self.elevate = elevate

    def occupied_slots(self, device: Path) -> set[int]:
        self._validate(device)
        result = self.runner(
            [
                *([str(self.pkexec_path)] if self.elevate else []),
                str(self.cryptsetup_path),
                "luksDump",
                "--dump-json-metadata",
                "--type",
                "luks2",
                str(device),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise DeviceError("could not inspect LUKS2 keyslots")
        try:
            value = json.loads(result.stdout.decode("utf-8"))
            slots = value["keyslots"]
            if not isinstance(slots, dict):
                raise TypeError
            return {int(slot) for slot in slots}
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise DeviceError("cryptsetup returned invalid LUKS2 metadata") from error

    def add_key(
        self,
        device: Path,
        *,
        existing_credential: bytes | bytearray,
        new_credential: bytes | bytearray,
        keyslot: int,
    ) -> None:
        if not 1 <= keyslot <= 31:
            raise DeviceError("refusing to enroll an invalid LUKS2 keyslot")
        self._validate(device)
        self._run_with_fifos(
            device,
            existing_credential=existing_credential,
            new_credential=new_credential,
            arguments=[
                "luksAddKey",
                "--type",
                "luks2",
                "--key-file",
                "{existing}",
                "--keyfile-size",
                str(len(existing_credential)),
                "--new-keyfile",
                "{new}",
                "--new-keyfile-size",
                str(len(new_credential)),
                "--new-key-slot",
                str(keyslot),
                str(device),
            ],
        )

    def remove_key(
        self, device: Path, credential: bytes | bytearray, *, keyslot: int
    ) -> None:
        if not 1 <= keyslot <= 31:
            raise DeviceError("refusing to remove a reserved or invalid LUKS2 keyslot")
        self._validate(device)
        before = self.occupied_slots(device)
        if keyslot not in before:
            raise DeviceError(f"LUKS2 keyslot {keyslot} is not occupied")
        if len(before) <= 1:
            raise DeviceError("refusing to remove the final LUKS2 keyslot")
        # Prove the supplied secret belongs to the declared slot before asking
        # luksRemoveKey to remove the slot matching that secret. This prevents a
        # corrupted Secret Service item from ever selecting reserved slot 0.
        self._run_with_fifos(
            device,
            existing_credential=credential,
            new_credential=None,
            arguments=[
                "open",
                "--type",
                "luks2",
                "--test-passphrase",
                "--key-slot",
                str(keyslot),
                "--key-file",
                "{existing}",
                "--keyfile-size",
                str(len(credential)),
                str(device),
            ],
        )
        self._run_with_fifos(
            device,
            existing_credential=credential,
            new_credential=None,
            arguments=[
                "luksRemoveKey",
                "--type",
                "luks2",
                "--keyfile-size",
                str(len(credential)),
                str(device),
                "{existing}",
            ],
        )
        after = self.occupied_slots(device)
        if after != before - {keyslot}:
            raise DeviceError(f"cryptsetup did not remove only LUKS2 keyslot {keyslot}")

    def _run_with_fifos(
        self,
        device: Path,
        *,
        existing_credential: bytes | bytearray,
        new_credential: bytes | bytearray | None,
        arguments: list[str],
    ) -> None:
        # An elevated helper must never create credential FIFOs below the
        # invoking user's writable runtime directory.
        parent = runtime_directory() if self.elevate else Path("/run")
        directory = Path(tempfile.mkdtemp(prefix="credentials-", dir=parent))
        os.chmod(directory, 0o700)
        existing_path = directory / "existing"
        new_path = directory / "new"
        paths = [(existing_path, existing_credential)]
        if new_credential is not None:
            paths.append((new_path, new_credential))
        for path, _secret in paths:
            os.mkfifo(path, 0o600)

        failures: list[BaseException] = []

        def write_fifo(path: Path, secret: bytes | bytearray) -> None:
            try:
                descriptor = os.open(path, os.O_WRONLY)
                try:
                    view = memoryview(secret)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                finally:
                    os.close(descriptor)
            except BaseException as error:  # propagated on the supervising thread
                failures.append(error)

        threads = [
            threading.Thread(target=write_fifo, args=item, daemon=True) for item in paths
        ]
        for thread in threads:
            thread.start()
        command = [
            *([str(self.pkexec_path)] if self.elevate else []),
            str(self.cryptsetup_path),
        ] + [
            str(existing_path)
            if item == "{existing}"
            else str(new_path)
            if item == "{new}"
            else item
            for item in arguments
        ]
        try:
            result = self.runner(command, check=False)
            if result.returncode != 0:
                raise DeviceError("cryptsetup keyslot operation was cancelled or failed")
        finally:
            # Release a writer if cryptsetup exited before opening one of the FIFOs.
            readers: list[int] = []
            for path, _secret in paths:
                with suppress(OSError):
                    readers.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            for thread in threads:
                thread.join(timeout=2)
                if thread.is_alive():
                    failures.append(TimeoutError("credential FIFO writer did not finish"))
            for descriptor in readers:
                os.close(descriptor)
            for path, _secret in paths:
                path.unlink(missing_ok=True)
            directory.rmdir()
        if failures:
            raise DeviceError("could not stream a credential to cryptsetup") from failures[0]

    def _validate(self, device: Path) -> None:
        executables = (
            (self.pkexec_path, self.cryptsetup_path)
            if self.elevate
            else (self.cryptsetup_path,)
        )
        for executable in executables:
            _validate_system_executable(executable)
        try:
            metadata = device.stat()
        except OSError as error:
            raise DeviceError(f"encrypted device is unavailable: {device}") from error
        if not stat.S_ISBLK(metadata.st_mode) or not str(device.resolve()).startswith("/dev/"):
            raise DeviceError("refusing a LUKS operation on a non-block device")


def _system_executable(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        raise DeviceError(f"required system command is unavailable: {name}")
    return Path(value).resolve()


def _validate_system_executable(path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise DeviceError(f"cannot validate privileged executable: {path}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise DeviceError(f"refusing unsafe privileged executable: {resolved}")
