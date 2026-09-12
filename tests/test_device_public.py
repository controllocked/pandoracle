from __future__ import annotations

import errno
import struct
from pathlib import Path

import pytest

from pandoracle.device_public import (
    _FAT_ATTRIBUTE_HIDDEN,
    _FAT_ATTRIBUTE_SYSTEM,
    _FAT_IOCTL_GET_ATTRIBUTES,
    _FAT_IOCTL_SET_ATTRIBUTES,
    ensure_public_marker_hidden,
    inspect_public_directory,
    replace_public_contents,
)
from pandoracle.errors import DeviceError


def test_public_marker_is_given_fat_hidden_and_system_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".pandoracle-device").mkdir()
    archive_attribute = 0x20
    written: list[int] = []

    def fake_ioctl(
        _descriptor: int, command: int, value: bytearray, _mutate: bool
    ) -> int:
        if command == _FAT_IOCTL_GET_ATTRIBUTES:
            value[:] = struct.pack("=I", archive_attribute)
        elif command == _FAT_IOCTL_SET_ATTRIBUTES:
            written.append(struct.unpack("=I", value)[0])
        return 0

    monkeypatch.setattr("pandoracle.device_public.fcntl.ioctl", fake_ioctl)

    assert ensure_public_marker_hidden(tmp_path)
    assert written == [archive_attribute | _FAT_ATTRIBUTE_HIDDEN | _FAT_ATTRIBUTE_SYSTEM]


def test_public_marker_attribute_is_cosmetic_and_unsupported_filesystems_are_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".pandoracle-device").mkdir()

    def unsupported(*_args: object) -> int:
        raise OSError(errno.ENOTTY, "not a FAT filesystem")

    monkeypatch.setattr("pandoracle.device_public.fcntl.ioctl", unsupported)

    assert not ensure_public_marker_hidden(tmp_path)


def test_public_directory_replaces_the_complete_visible_hierarchy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "PRIVATE_BACKUP").mkdir(parents=True)
    (source / "README.txt").write_text("Return to owner\n", encoding="utf-8")
    (source / "PRIVATE_BACKUP" / "note.bin").write_bytes(b"synthetic\x00content")
    (source / ".visible-on-fat").write_text("public", encoding="utf-8")
    public = tmp_path / "public"
    marker = public / ".pandoracle-device" / "device.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"marker": true}\n', encoding="utf-8")
    (public / "old.txt").write_text("old", encoding="utf-8")

    result = replace_public_contents(public, source)

    assert result.files == 3
    assert result.directories == 1
    assert result.bytes == sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
    assert (public / "README.txt").read_text(encoding="utf-8") == "Return to owner\n"
    assert (public / "PRIVATE_BACKUP" / "note.bin").read_bytes() == b"synthetic\x00content"
    assert (public / ".visible-on-fat").read_text(encoding="utf-8") == "public"
    assert marker.read_text(encoding="utf-8") == '{"marker": true}\n'
    assert not (public / "old.txt").exists()
    assert not list(public.glob(".pandoracle-public-update-*"))


def test_public_directory_rejects_reserved_marker_and_symlinks(tmp_path: Path) -> None:
    reserved = tmp_path / "reserved"
    (reserved / ".PANDORACLE-DEVICE").mkdir(parents=True)
    with pytest.raises(DeviceError, match="reserved .pandoracle-device"):
        inspect_public_directory(reserved)

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "target.txt").write_text("public", encoding="utf-8")
    (linked / "alias.txt").symlink_to("target.txt")
    with pytest.raises(DeviceError, match="must not contain symbolic links"):
        inspect_public_directory(linked)


def test_public_directory_must_not_be_on_the_target_partition(tmp_path: Path) -> None:
    public = tmp_path / "public"
    source = public / "source"
    source.mkdir(parents=True)
    (source / "file.txt").write_text("public", encoding="utf-8")

    with pytest.raises(DeviceError, match="must not be inside"):
        replace_public_contents(public, source)


def test_copy_failure_keeps_existing_public_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.txt").write_text("new", encoding="utf-8")
    public = tmp_path / "public"
    public.mkdir()
    (public / "old.txt").write_text("old", encoding="utf-8")

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic read failure")

    monkeypatch.setattr("pandoracle.device_public.shutil.copyfileobj", fail_copy)

    with pytest.raises(DeviceError, match="cannot replace public contents"):
        replace_public_contents(public, source)

    assert (public / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (public / "new.txt").exists()
    assert not list(public.glob(".pandoracle-public-update-*"))
