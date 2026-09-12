from __future__ import annotations

import fcntl
import os
import shutil
import stat
import struct
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pandoracle.device_models import PUBLIC_MARKER
from pandoracle.errors import DeviceError
from pandoracle.fs import fsync_directory

# linux/msdos_fs.h: FAT_IOCTL_{GET,SET}_ATTRIBUTES and DOS directory attributes.
# The Pandora device client is Linux-only and its public filesystem is FAT32.
_FAT_IOCTL_GET_ATTRIBUTES = 0x80047210
_FAT_IOCTL_SET_ATTRIBUTES = 0x40047211
_FAT_ATTRIBUTE_HIDDEN = 0x02
_FAT_ATTRIBUTE_SYSTEM = 0x04
_UINT32 = struct.Struct("=I")
_RESERVED_DIRECTORY = PUBLIC_MARKER.parts[0]


@dataclass(frozen=True)
class PublicContentSummary:
    files: int
    directories: int
    bytes: int


def inspect_public_directory(source: Path) -> PublicContentSummary:
    """Validate a portable public-content hierarchy and summarize it."""

    source = source.expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as error:
        raise DeviceError(f"cannot access public content directory: {error}") from error
    if not source.is_dir():
        raise DeviceError(f"public content source is not a directory: {source}")

    files = 0
    directories = 0
    total_bytes = 0

    def visit(directory: Path, *, top_level: bool = False) -> None:
        nonlocal files, directories, total_bytes
        try:
            entries = list(directory.iterdir())
        except OSError as error:
            raise DeviceError(f"cannot read public content directory: {error}") from error
        for entry in entries:
            if top_level and entry.name.casefold() == _RESERVED_DIRECTORY.casefold():
                raise DeviceError(
                    f"public content directory must not contain reserved {_RESERVED_DIRECTORY}"
                )
            try:
                metadata = entry.lstat()
            except OSError as error:
                raise DeviceError(f"cannot inspect public content {entry}: {error}") from error
            mode = metadata.st_mode
            if stat.S_ISLNK(mode):
                raise DeviceError(f"public content must not contain symbolic links: {entry}")
            if stat.S_ISDIR(mode):
                directories += 1
                visit(entry)
            elif stat.S_ISREG(mode):
                files += 1
                total_bytes += metadata.st_size
            else:
                raise DeviceError(
                    f"public content must contain only files and directories: {entry}"
                )

    visit(source, top_level=True)
    return PublicContentSummary(files, directories, total_bytes)


def replace_public_contents(root: Path, source: Path) -> PublicContentSummary:
    """Replace user-visible public contents while preserving the device marker."""

    summary = inspect_public_directory(source)
    source = source.expanduser().resolve(strict=True)
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise DeviceError(f"cannot access Pandora public partition: {error}") from error
    if source == root or root in source.parents:
        raise DeviceError("public content source must not be inside the Pandora public partition")

    staging = root / f".pandoracle-public-update-{uuid.uuid4().hex}"
    try:
        staging.mkdir(mode=0o700)
        _copy_directory(source, staging)

        for current in root.iterdir():
            if current == staging or current.name == _RESERVED_DIRECTORY:
                continue
            _remove_tree_entry(current)
        for prepared in staging.iterdir():
            os.replace(prepared, root / prepared.name)
        staging.rmdir()
        fsync_directory(root)
    except (OSError, DeviceError) as error:
        with suppress(OSError):
            shutil.rmtree(staging)
        if isinstance(error, DeviceError):
            raise
        raise DeviceError(f"cannot replace public contents: {error}") from error
    return summary


def _copy_directory(source: Path, destination: Path) -> None:
    for entry in source.iterdir():
        mode = entry.lstat().st_mode
        target = destination / entry.name
        if stat.S_ISLNK(mode):
            raise DeviceError(f"public content must not contain symbolic links: {entry}")
        if stat.S_ISDIR(mode):
            target.mkdir()
            _copy_directory(entry, target)
            continue
        if not stat.S_ISREG(mode):
            raise DeviceError(f"public content must contain only files and directories: {entry}")
        with entry.open("rb") as input_stream, target.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())


def _remove_tree_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def ensure_public_marker_hidden(root: Path) -> bool:
    """Apply FAT Hidden/System attributes to the technical marker directory.

    This is deliberately best-effort: the marker remains non-sensitive, and a
    cosmetic attribute failure must not make an otherwise usable device fail.
    """

    marker_directory = root / PUBLIC_MARKER.parent
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(marker_directory, flags)
    except OSError:
        return False
    try:
        encoded = bytearray(_UINT32.size)
        fcntl.ioctl(descriptor, _FAT_IOCTL_GET_ATTRIBUTES, encoded, True)
        (current,) = _UINT32.unpack(encoded)
        wanted = current | _FAT_ATTRIBUTE_HIDDEN | _FAT_ATTRIBUTE_SYSTEM
        if wanted != current:
            fcntl.ioctl(
                descriptor,
                _FAT_IOCTL_SET_ATTRIBUTES,
                bytearray(_UINT32.pack(wanted)),
                True,
            )
            with suppress(OSError):
                os.fsync(descriptor)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)
