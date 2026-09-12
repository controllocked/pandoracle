from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from pandoracle.device_credentials import (
    LuksKeyslotManager,
    generate_host_credential,
    generate_recovery_credential,
)
from pandoracle.errors import DeviceError


def test_generated_credentials_are_independent_256_bit_values() -> None:
    recovery = generate_recovery_credential()
    host_a = generate_host_credential()
    host_b = generate_host_credential()

    groups = bytes(recovery).decode("ascii").split("-")
    assert len(b"".join(group.encode("ascii") for group in groups)) == 52
    assert all(len(group) == 4 for group in groups[:-1])
    assert len(host_a) == 43
    assert host_a != host_b
    assert recovery != host_a


def test_cryptsetup_receives_credentials_only_through_fifos(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    existing = b"main passphrase"
    new = b"host-only-random-key"
    captured: list[bytes] = []
    command_seen: list[str] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        command_seen.extend(command)
        fifo_paths = [Path(item) for item in command if "credentials-" in item]
        assert len(fifo_paths) == 2
        for path in fifo_paths:
            assert stat.S_ISFIFO(path.stat().st_mode)
            with path.open("rb", buffering=0) as stream:
                captured.append(stream.read())
        return subprocess.CompletedProcess(command, 0)

    manager = LuksKeyslotManager(
        runner=runner,
        pkexec_path=Path("/usr/bin/pkexec"),
        cryptsetup_path=Path("/usr/sbin/cryptsetup"),
    )
    manager._run_with_fifos(
        Path("/dev/test"),
        existing_credential=existing,
        new_credential=new,
        arguments=["luksAddKey", "{existing}", "{new}"],
    )

    assert captured == [existing, new]
    assert existing.decode() not in command_seen
    assert new.decode() not in command_seen
    assert not list((runtime / "pandoracle").glob("credentials-*"))
    assert all(not path.is_file() for path in (runtime / "pandoracle").iterdir())
    assert "PANDORACLE_PASSPHRASE" not in os.environ


def test_removal_targets_only_the_declared_non_main_keyslot() -> None:
    class Manager(LuksKeyslotManager):
        def __init__(self) -> None:
            self.states = iter(({0, 1, 2}, {0, 1}))
            self.calls: list[list[str]] = []

        def _validate(self, _device: Path) -> None:
            return None

        def occupied_slots(self, _device: Path) -> set[int]:
            return set(next(self.states))

        def _run_with_fifos(self, _device: Path, **kwargs: object) -> None:
            self.calls.append(list(kwargs["arguments"]))  # type: ignore[arg-type]

    manager = Manager()
    manager.remove_key(Path("/dev/fake"), b"host", keyslot=2)
    assert manager.calls[0][0] == "open"
    assert manager.calls[0][manager.calls[0].index("--key-slot") + 1] == "2"
    assert manager.calls[1][0] == "luksRemoveKey"
    assert manager.calls[1][-1] == "{existing}"

    with pytest.raises(DeviceError, match="reserved or invalid"):
        Manager().remove_key(Path("/dev/fake"), b"owner", keyslot=0)


def test_key_addition_is_one_privileged_cryptsetup_operation() -> None:
    class Manager(LuksKeyslotManager):
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def _validate(self, _device: Path) -> None:
            return None

        def occupied_slots(self, _device: Path) -> set[int]:
            pytest.fail("luksAddKey selects and validates the requested free slot")

        def _run_with_fifos(self, _device: Path, **kwargs: object) -> None:
            self.calls.append(list(kwargs["arguments"]))  # type: ignore[arg-type]

    manager = Manager()
    manager.add_key(
        Path("/dev/fake"),
        existing_credential=b"owner",
        new_credential=b"host",
        keyslot=2,
    )

    assert len(manager.calls) == 1
    assert manager.calls[0][0] == "luksAddKey"
    assert manager.calls[0][manager.calls[0].index("--new-key-slot") + 1] == "2"
