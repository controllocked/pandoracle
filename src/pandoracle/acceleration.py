from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from pandoracle.fs import fsync_file
from pandoracle.models import AccelerationKind

ACCELERATION_FORMAT_VERSION = 1
MAX_ROWSET_ORDINALS = (64 * 1024 * 1024) // 8

_SCHEMA = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE postings (
    field_id INTEGER NOT NULL,
    key TEXT COLLATE BINARY NOT NULL,
    record_ordinal INTEGER NOT NULL CHECK(record_ordinal >= 0),
    PRIMARY KEY(field_id, key, record_ordinal)
) WITHOUT ROWID;
CREATE TABLE field_statistics (
    field_id INTEGER PRIMARY KEY,
    posting_count INTEGER NOT NULL,
    key_bytes INTEGER NOT NULL
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class AccelerationPosting:
    field_id: int
    key: str
    record_ordinal: int


class AccelerationWriter:
    def __init__(
        self,
        path: Path,
        *,
        primitive: AccelerationKind,
        dataset_version_id: str,
        canonical_profile_id: str,
        index_profile_id: str,
        policy_fingerprint: str,
    ) -> None:
        self.path = path
        self.primitive = primitive
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        self.connection.executescript(_SCHEMA)
        self.connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (
                ("format_version", str(ACCELERATION_FORMAT_VERSION)),
                ("primitive", primitive.value),
                ("dataset_version_id", dataset_version_id),
                ("canonical_profile_id", canonical_profile_id),
                ("index_profile_id", index_profile_id),
                ("policy_fingerprint", policy_fingerprint),
            ),
        )

    def add(self, postings: Iterable[AccelerationPosting]) -> None:
        def rows() -> Iterator[tuple[int, str, int]]:
            for posting in postings:
                yield posting.field_id, posting.key, posting.record_ordinal

        self.connection.executemany(
            "INSERT OR IGNORE INTO postings(field_id, key, record_ordinal) VALUES (?, ?, ?)",
            rows(),
        )

    def finish(self) -> tuple[dict[int, dict[str, int]], dict[str, float]]:
        # INSERT OR IGNORE can remove duplicate tokens. Recalculate authoritative counts.
        statistics_started = time.perf_counter()
        statistics: dict[int, dict[str, int]] = {}
        for field_id, count, key_bytes in self.connection.execute(
            "SELECT field_id, COUNT(*), COALESCE(SUM(length(CAST(key AS BLOB))), 0) "
            "FROM postings GROUP BY field_id"
        ):
            statistics[int(field_id)] = {
                "posting_count": int(count),
                "key_bytes": int(key_bytes),
            }
        self.connection.executemany(
            "INSERT INTO field_statistics(field_id, posting_count, key_bytes) VALUES (?, ?, ?)",
            (
                (field_id, value["posting_count"], value["key_bytes"])
                for field_id, value in statistics.items()
            ),
        )
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('posting_count', ?)",
            (str(sum(item["posting_count"] for item in statistics.values())),),
        )
        statistics_seconds = time.perf_counter() - statistics_started
        commit_started = time.perf_counter()
        self.connection.commit()
        commit_seconds = time.perf_counter() - commit_started
        integrity_started = time.perf_counter()
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        integrity_seconds = time.perf_counter() - integrity_started
        if integrity != "ok":
            raise RuntimeError(f"acceleration integrity check failed: {integrity}")
        self.connection.close()
        fsync_started = time.perf_counter()
        fsync_file(self.path)
        fsync_seconds = time.perf_counter() - fsync_started
        return statistics, {
            "statistics_seconds": statistics_seconds,
            "commit_seconds": commit_seconds,
            "integrity_check_seconds": integrity_seconds,
            "fsync_seconds": fsync_seconds,
            "total_seconds": (
                statistics_seconds + commit_seconds + integrity_seconds + fsync_seconds
            ),
        }

    def abort(self) -> None:
        self.connection.close()


def _open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def verify_acceleration(
    path: Path,
    *,
    primitive: AccelerationKind,
    dataset_version_id: str,
    canonical_profile_id: str,
    index_profile_id: str,
    policy_fingerprint: str,
    check_integrity: bool = True,
) -> None:
    with contextlib.closing(_open(path)) as connection:
        integrity = (
            connection.execute("PRAGMA integrity_check").fetchone()[0]
            if check_integrity
            else "ok"
        )
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    if integrity != "ok":
        raise RuntimeError(f"acceleration integrity check failed: {integrity}")
    expected = {
        "format_version": str(ACCELERATION_FORMAT_VERSION),
        "primitive": primitive.value,
        "dataset_version_id": dataset_version_id,
        "canonical_profile_id": canonical_profile_id,
        "index_profile_id": index_profile_id,
        "policy_fingerprint": policy_fingerprint,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise RuntimeError("acceleration metadata does not match its catalog profile")


def query_value_ordinals(
    path: Path,
    field_ids: Sequence[int],
    lower: str,
    upper: str,
    *,
    limit: int = MAX_ROWSET_ORDINALS + 1,
) -> pa.UInt64Array:
    if not field_ids:
        return pa.array([], type=pa.uint64())
    statements = []
    parameters: list[object] = []
    for field_id in sorted(set(field_ids)):
        statements.append(
            "SELECT record_ordinal FROM postings WHERE field_id=? AND key>=? AND key<=?"
        )
        parameters.extend((field_id, lower, upper))
    sql = "SELECT record_ordinal FROM (" + " UNION ".join(statements) + ") "
    sql += "ORDER BY record_ordinal LIMIT ?"
    parameters.append(limit)
    with contextlib.closing(_open(path)) as connection:
        values = [int(row[0]) for row in connection.execute(sql, parameters)]
    return pa.array(values, type=pa.uint64())


def count_value_ordinals(
    path: Path,
    field_ids: Sequence[int],
    lower: str,
    upper: str,
) -> int:
    unique_fields = sorted(set(field_ids))
    if not unique_fields:
        return 0
    placeholders = ",".join("?" for _ in unique_fields)
    sql = (
        "SELECT COUNT(DISTINCT record_ordinal) FROM postings "
        f"WHERE field_id IN ({placeholders}) AND key>=? AND key<=?"
    )
    with contextlib.closing(_open(path)) as connection:
        row = connection.execute(sql, (*unique_fields, lower, upper)).fetchone()
    return int(row[0])


def query_token_ordinals(
    path: Path,
    field_ids: Sequence[int],
    tokens: Sequence[str],
    *,
    limit: int = MAX_ROWSET_ORDINALS + 1,
) -> pa.UInt64Array:
    unique_tokens = sorted(set(tokens))
    if not field_ids or not unique_tokens:
        return pa.array([], type=pa.uint64())
    field_placeholders = ",".join("?" for _ in set(field_ids))
    token_placeholders = ",".join("?" for _ in unique_tokens)
    sql = (
        "SELECT record_ordinal FROM ("
        "SELECT field_id, record_ordinal FROM postings "
        f"WHERE field_id IN ({field_placeholders}) AND key IN ({token_placeholders}) "
        "GROUP BY field_id, record_ordinal HAVING COUNT(DISTINCT key)=?"
        ") GROUP BY record_ordinal ORDER BY record_ordinal LIMIT ?"
    )
    parameters: list[object] = [
        *sorted(set(field_ids)),
        *unique_tokens,
        len(unique_tokens),
        limit,
    ]
    with contextlib.closing(_open(path)) as connection:
        values = [int(row[0]) for row in connection.execute(sql, parameters)]
    return pa.array(values, type=pa.uint64())


def count_token_ordinals(
    path: Path,
    field_ids: Sequence[int],
    tokens: Sequence[str],
) -> int:
    unique_fields = sorted(set(field_ids))
    unique_tokens = sorted(set(tokens))
    if not unique_fields or not unique_tokens:
        return 0
    field_placeholders = ",".join("?" for _ in unique_fields)
    token_placeholders = ",".join("?" for _ in unique_tokens)
    sql = (
        "SELECT COUNT(DISTINCT record_ordinal) FROM ("
        "SELECT field_id, record_ordinal FROM postings "
        f"WHERE field_id IN ({field_placeholders}) AND key IN ({token_placeholders}) "
        "GROUP BY field_id, record_ordinal HAVING COUNT(DISTINCT key)=?"
        ")"
    )
    parameters: list[object] = [*unique_fields, *unique_tokens, len(unique_tokens)]
    with contextlib.closing(_open(path)) as connection:
        row = connection.execute(sql, parameters).fetchone()
    return int(row[0])


def intersect_rowsets(left: pa.UInt64Array, right: pa.UInt64Array) -> pa.UInt64Array:
    if len(left) > len(right):
        left, right = right, left
    if not left or not right:
        return pa.array([], type=pa.uint64())
    return pc.filter(left, pc.is_in(left, value_set=right))
