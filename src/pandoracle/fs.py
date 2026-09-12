from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

COPY_BUFFER_SIZE = 4 * 1024 * 1024


def utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_atomic(path, encoded)


def write_text_atomic(path: Path, value: str) -> None:
    _write_atomic(path, value.encode("utf-8"))


def _write_atomic(path: Path, encoded: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def hash_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(COPY_BUFFER_SIZE):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def hash_file(path: Path) -> tuple[str, int]:
    with path.open("rb") as stream:
        return hash_stream(stream)


def copy_and_hash(
    source: Path,
    destination: Path,
    progress: Callable[[int], None] | None = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(destination, flags, 0o600)
    try:
        with (
            source.open("rb") as input_stream,
            os.fdopen(descriptor, "wb", closefd=False) as output_stream,
        ):
            while chunk := input_stream.read(COPY_BUFFER_SIZE):
                output_stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                if progress is not None:
                    progress(size)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size
