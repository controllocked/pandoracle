from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import statistics
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pandoracle.errors import WorkspaceError
from pandoracle.fs import fsync_directory, fsync_file, utc_now
from pandoracle.schema import CustomTypeSpec

CATALOG_SCHEMA_VERSION = 5


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    active_version_id TEXT,
    active_canonical_profile_id TEXT,
    active_index_profile_id TEXT
);

CREATE TABLE IF NOT EXISTS source_blobs (
    sha256 TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    stored_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    created_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS dataset_versions (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(id),
    source_sha256 TEXT NOT NULL REFERENCES source_blobs(sha256),
    source_name TEXT NOT NULL,
    status TEXT NOT NULL,
    row_count INTEGER,
    schema_json TEXT NOT NULL,
    recipe_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    parent_version_id TEXT REFERENCES dataset_versions(id),
    schema_fingerprint TEXT,
    normalization_stats_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS dataset_versions_by_dataset
ON dataset_versions(dataset_id, created_at);

CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    dataset_id TEXT,
    dataset_version_id TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    dataset_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    kind TEXT NOT NULL,
    format_version INTEGER NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    created_at TEXT NOT NULL,
    canonical_profile_id TEXT,
    index_profile_id TEXT
);

CREATE INDEX IF NOT EXISTS artifacts_by_version_kind
ON artifacts(dataset_version_id, kind);

CREATE TABLE IF NOT EXISTS custom_semantic_types (
    type_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    normalizer_id TEXT NOT NULL,
    normalizer_version INTEGER NOT NULL,
    default_operator TEXT NOT NULL DEFAULT 'EXACT'
        CHECK(default_operator IN ('EXACT', 'TOKEN')),
    created_at TEXT NOT NULL,
    retired_at TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS canonical_profiles (
    id TEXT PRIMARY KEY,
    dataset_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    parent_profile_id TEXT REFERENCES canonical_profiles(id),
    status TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    statistics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE INDEX IF NOT EXISTS canonical_profiles_by_version
ON canonical_profiles(dataset_version_id, created_at);

CREATE TABLE IF NOT EXISTS index_profiles (
    id TEXT PRIMARY KEY,
    dataset_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    canonical_profile_id TEXT NOT NULL REFERENCES canonical_profiles(id),
    parent_profile_id TEXT REFERENCES index_profiles(id),
    status TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    policy_version INTEGER NOT NULL DEFAULT 1,
    statistics_json TEXT NOT NULL DEFAULT '{}',
    build_metrics_json TEXT NOT NULL DEFAULT '{}',
    posting_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE INDEX IF NOT EXISTS index_profiles_by_version
ON index_profiles(dataset_version_id, created_at);

CREATE TABLE IF NOT EXISTS performance_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workload TEXT NOT NULL,
    format_version INTEGER NOT NULL,
    row_count INTEGER NOT NULL CHECK(row_count >= 0),
    byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
    elapsed_seconds REAL NOT NULL CHECK(elapsed_seconds > 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS performance_samples_lookup
ON performance_samples(workload, format_version, created_at DESC);
"""


class Catalog:
    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _configure(connection: sqlite3.Connection, *, read_only: bool) -> None:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if not read_only:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA journal_mode = DELETE")

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(self.path)
        self._configure(connection, read_only=read_only)
        return connection

    def initialize(self, workspace_id: str) -> None:
        connection = self.connect()
        try:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                ("catalog_schema_version", str(CATALOG_SCHEMA_VERSION)),
            )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                ("workspace_id", workspace_id),
            )
            connection.commit()
        finally:
            connection.close()

    def _backup(self, backup_root: Path, label: str) -> Path:
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_path = backup_root / f"catalog-{label}-{uuid.uuid4()}.sqlite"
        source = self.connect(read_only=True)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        os.chmod(backup_path, 0o600)
        fsync_file(backup_path)
        fsync_directory(backup_root)
        return backup_path

    def migrate(self, backup_root: Path, workspace_id: str) -> bool:
        """Upgrade only the final pre-release catalog after a durable backup."""
        try:
            with contextlib.closing(self.connect(read_only=True)) as connection:
                rows = dict(connection.execute("SELECT key, value FROM metadata"))
        except sqlite3.Error as error:
            raise WorkspaceError(f"cannot open workspace catalog: {error}") from error
        if rows.get("workspace_id") != workspace_id:
            raise WorkspaceError("manifest and catalog workspace identities differ")
        try:
            version = int(rows.get("catalog_schema_version", -1))
        except (TypeError, ValueError) as error:
            raise WorkspaceError("workspace catalog has an invalid schema version") from error
        if version not in {4, CATALOG_SCHEMA_VERSION}:
            raise WorkspaceError(
                f"unsupported pre-v1 catalog schema {version}; "
                "create a new v1 workspace with 'pandoracle init PATH'"
            )
        try:
            with contextlib.closing(self.connect(read_only=True)) as connection:
                exact_artifacts = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM artifacts WHERE kind='EXACT_INDEX'"
                    ).fetchone()[0]
                )
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise WorkspaceError(f"cannot validate workspace catalog: {error}") from error
        if exact_artifacts:
            raise WorkspaceError(
                "pre-v1 EXACT acceleration is unsupported; "
                "create a new v1 workspace with 'pandoracle init PATH'"
            )
        if version == CATALOG_SCHEMA_VERSION:
            return False
        try:
            self._backup(backup_root, "v4")
            with self.transaction() as connection:
                connection.execute(
                    "ALTER TABLE custom_semantic_types ADD COLUMN retired_at TEXT"
                )
                profiles = connection.execute(
                    "SELECT id, policy_json, build_metrics_json FROM index_profiles"
                ).fetchall()
                for row in profiles:
                    policy = json.loads(row["policy_json"])
                    if not isinstance(policy, dict):
                        raise ValueError("acceleration policy is not an object")
                    policy["policy_version"] = 1
                    metrics = json.loads(row["build_metrics_json"] or "{}")
                    if not isinstance(metrics, dict):
                        raise ValueError("acceleration build metrics are not an object")
                    if metrics:
                        metrics["metrics_version"] = 1
                    connection.execute(
                        """
                        UPDATE index_profiles
                        SET policy_json=?, policy_version=?, build_metrics_json=?
                        WHERE id=?
                        """,
                        (
                            json.dumps(policy, sort_keys=True),
                            1,
                            json.dumps(metrics, sort_keys=True),
                            row["id"],
                        ),
                    )
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='catalog_schema_version'",
                    (str(CATALOG_SCHEMA_VERSION),),
                )
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
            raise WorkspaceError(
                "pre-v1 workspace catalog cannot be upgraded; "
                "create a new v1 workspace with 'pandoracle init PATH'"
            ) from error
        return True

    def validate(self, workspace_id: str) -> None:
        try:
            with contextlib.closing(self.connect(read_only=True)) as connection:
                rows = dict(connection.execute("SELECT key, value FROM metadata"))
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as error:
            raise WorkspaceError(f"cannot open workspace catalog: {error}") from error
        if integrity != "ok":
            raise WorkspaceError(f"catalog integrity check failed: {integrity}")
        if rows.get("workspace_id") != workspace_id:
            raise WorkspaceError("manifest and catalog workspace identities differ")
        try:
            version = int(rows.get("catalog_schema_version", -1))
        except (TypeError, ValueError) as error:
            raise WorkspaceError("workspace catalog has an invalid schema version") from error
        if version != CATALOG_SCHEMA_VERSION:
            raise WorkspaceError(
                f"unsupported catalog schema {version}; expected {CATALOG_SCHEMA_VERSION}"
            )

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_operation_status(
        self, operation_id: str, status: str, error: str | None = None
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE operations SET status = ?, updated_at = ?, error = ? WHERE id = ?",
                (status, utc_now(), error, operation_id),
            )

    def list_sources(self) -> list[dict[str, Any]]:
        query = """
            SELECT d.id AS dataset_id, d.name, d.active_version_id,
                   d.active_canonical_profile_id, d.active_index_profile_id,
                   v.source_name, v.source_sha256, v.row_count, v.published_at,
                   v.schema_json, v.normalization_stats_json,
                   cp.spec_json AS canonical_spec_json,
                   ip.policy_json AS index_policy_json
            FROM datasets d
            LEFT JOIN dataset_versions v ON v.id = d.active_version_id
            LEFT JOIN canonical_profiles cp ON cp.id = d.active_canonical_profile_id
            LEFT JOIN index_profiles ip ON ip.id = d.active_index_profile_id
            ORDER BY d.name
        """
        with contextlib.closing(self.connect(read_only=True)) as connection:
            result = []
            for row in connection.execute(query):
                item = dict(row)
                item["schema"] = json.loads(item.pop("schema_json") or "[]")
                item["normalization_stats"] = json.loads(
                    item.pop("normalization_stats_json") or "{}"
                )
                item["canonical_fields"] = json.loads(item.pop("canonical_spec_json") or "[]")
                policy = json.loads(item.pop("index_policy_json") or "{}")
                item["index_policies"] = policy.get("fields", [])
                result.append(item)
            return result

    def find_dataset(self, identifier: str) -> dict[str, Any] | None:
        query = """
            SELECT d.id AS dataset_id, d.name, d.active_version_id,
                   d.active_canonical_profile_id, d.active_index_profile_id,
                   v.source_name, v.source_sha256, v.row_count, v.published_at,
                   v.schema_json, v.recipe_json, v.normalization_stats_json,
                   cp.spec_json AS canonical_spec_json,
                   ip.policy_json AS index_policy_json
            FROM datasets d
            LEFT JOIN dataset_versions v ON v.id = d.active_version_id
            LEFT JOIN canonical_profiles cp ON cp.id = d.active_canonical_profile_id
            LEFT JOIN index_profiles ip ON ip.id = d.active_index_profile_id
            WHERE d.id = ? OR d.name = ?
        """
        with contextlib.closing(self.connect(read_only=True)) as connection:
            row = connection.execute(query, (identifier, identifier)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["schema"] = json.loads(item.pop("schema_json") or "[]")
        item["recipe"] = json.loads(item.pop("recipe_json") or "{}")
        item["normalization_stats"] = json.loads(item.pop("normalization_stats_json") or "{}")
        item["canonical_fields"] = json.loads(item.pop("canonical_spec_json") or "[]")
        policy = json.loads(item.pop("index_policy_json") or "{}")
        item["index_policies"] = policy.get("fields", [])
        return item

    def list_custom_types(self, *, include_retired: bool = True) -> dict[str, CustomTypeSpec]:
        where = "" if include_retired else "WHERE retired_at IS NULL"
        with contextlib.closing(self.connect(read_only=True)) as connection:
            rows = connection.execute(
                f"""
                SELECT type_id, label, normalizer_id, normalizer_version, default_operator
                FROM custom_semantic_types {where} ORDER BY type_id
                """
            )
            return {
                str(row["type_id"]): CustomTypeSpec(
                    type_id=str(row["type_id"]),
                    label=str(row["label"]),
                    normalizer_id=str(row["normalizer_id"]),
                    normalizer_version=int(row["normalizer_version"]),
                    default_operator=str(row["default_operator"]),
                )
                for row in rows
            }

    def list_custom_type_rows(self) -> list[dict[str, Any]]:
        with contextlib.closing(self.connect(read_only=True)) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT type_id,label,normalizer_id,normalizer_version,"
                    "default_operator,created_at,retired_at "
                    "FROM custom_semantic_types ORDER BY type_id"
                )
            ]

    def set_custom_type_retired(self, type_id: str, *, retired: bool) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT retired_at FROM custom_semantic_types WHERE type_id=?", (type_id,)
            ).fetchone()
            if row is None:
                raise WorkspaceError(
                    f"custom semantic type not found: {type_id}; run 'pandoracle types'"
                )
            currently_retired = row["retired_at"] is not None
            if currently_retired == retired:
                return False
            connection.execute(
                "UPDATE custom_semantic_types SET retired_at=? WHERE type_id=?",
                (utc_now() if retired else None, type_id),
            )
            return True

    def record_performance_sample(
        self,
        workload: str,
        format_version: int,
        row_count: int,
        byte_count: int,
        elapsed_seconds: float,
    ) -> None:
        if elapsed_seconds < 1.0 or (byte_count < 64 * 1024 * 1024 and row_count < 1_000_000):
            return
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO performance_samples(workload,format_version,row_count,byte_count,"
                "elapsed_seconds,created_at) VALUES (?,?,?,?,?,?)",
                (workload, format_version, row_count, byte_count, elapsed_seconds, utc_now()),
            )
            old = connection.execute(
                "SELECT id FROM performance_samples WHERE workload=? AND format_version=? "
                "ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET 5",
                (workload, format_version),
            ).fetchall()
            if old:
                connection.executemany(
                    "DELETE FROM performance_samples WHERE id=?",
                    ((int(row[0]),) for row in old),
                )

    def performance_rates(self, workload: str, format_version: int) -> dict[str, float] | None:
        with contextlib.closing(self.connect(read_only=True)) as connection:
            rows = connection.execute(
                "SELECT row_count,byte_count,elapsed_seconds FROM performance_samples "
                "WHERE workload=? AND format_version=? ORDER BY created_at DESC,id DESC LIMIT 5",
                (workload, format_version),
            ).fetchall()
        if not rows:
            return None
        byte_rates = [float(row["byte_count"]) / float(row["elapsed_seconds"]) for row in rows]
        row_rates = [float(row["row_count"]) / float(row["elapsed_seconds"]) for row in rows]
        return {
            "bytes_per_second": statistics.median(byte_rates),
            "rows_per_second": statistics.median(row_rates),
            "sample_count": float(len(rows)),
        }
