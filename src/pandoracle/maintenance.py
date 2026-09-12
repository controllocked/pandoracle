from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pyarrow.parquet as pq

from pandoracle.acceleration import verify_acceleration
from pandoracle.errors import WorkspaceError
from pandoracle.faults import trigger_fault
from pandoracle.fs import fsync_directory, hash_file, utc_now
from pandoracle.models import AccelerationKind, OperationStatus
from pandoracle.workspace import Workspace

_TERMINAL_OPERATION_STATUSES = {
    OperationStatus.PUBLISHED.value,
    OperationStatus.FAILED.value,
    OperationStatus.ABORTED.value,
}
_FINAL_ROOTS = (
    PurePosixPath("datasets"),
    PurePosixPath("canonical/v1"),
    PurePosixPath("accelerations/v1"),
)


@dataclass(frozen=True)
class GarbageCollectionItem:
    kind: str
    reason: str
    relative_path: str | None = None
    catalog_id: str | None = None
    size_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GarbageCollectionPlan:
    blockers: tuple[str, ...]
    items: tuple[GarbageCollectionItem, ...]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items if item.relative_path is not None)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return {
            "gc_version": 1,
            "mode": "dry-run",
            "blockers": list(self.blockers),
            "items": [item.to_dict() for item in self.items],
            "totals": {
                "item_count": len(self.items),
                "reclaimable_bytes": self.reclaimable_bytes,
                "by_kind": counts,
            },
        }


@dataclass(frozen=True)
class GarbageCollectionResult:
    operation_id: str | None
    blockers: tuple[str, ...]
    items: tuple[GarbageCollectionItem, ...]

    @property
    def reclaimed_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items if item.relative_path is not None)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return {
            "gc_version": 1,
            "mode": "apply",
            "operation_id": self.operation_id,
            "blockers": list(self.blockers),
            "items": [item.to_dict() for item in self.items],
            "totals": {
                "item_count": len(self.items),
                "reclaimed_bytes": self.reclaimed_bytes,
                "by_kind": counts,
            },
        }


@dataclass(frozen=True)
class _Inventory:
    plan: GarbageCollectionPlan
    filesystem_paths: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    index_profile_ids: tuple[str, ...]
    canonical_profile_ids: tuple[str, ...]
    dataset_version_ids: tuple[str, ...]
    source_sha256s: tuple[str, ...]
    dataset_ids: tuple[str, ...]


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _relative_path(value: str, *, roots: tuple[PurePosixPath, ...]) -> PurePosixPath:
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise WorkspaceError(f"unsafe workspace-relative path: {value}")
    if not any(relative != root and relative.is_relative_to(root) for root in roots):
        raise WorkspaceError(f"path is outside a managed storage root: {value}")
    return relative


def _tree_size(path: Path) -> int:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise WorkspaceError(f"managed storage contains a symlink: {path}")
    if stat.S_ISREG(metadata.st_mode):
        return metadata.st_size
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceError(f"managed storage contains an unsupported file type: {path}")
    total = 0
    with os.scandir(path) as entries:
        for entry in entries:
            total += _tree_size(Path(entry.path))
    return total


def _files_below(path: Path) -> set[str]:
    files: set[str] = set()
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise WorkspaceError(f"managed storage contains a symlink: {path}")
    if stat.S_ISREG(metadata.st_mode):
        return {str(path)}
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceError(f"managed storage contains an unsupported file type: {path}")
    with os.scandir(path) as entries:
        for entry in entries:
            files.update(_files_below(Path(entry.path)))
    return files


def _managed_final_leaves(workspace: Workspace) -> tuple[list[Path], list[str]]:
    leaves: list[Path] = []
    blockers: list[str] = []
    layouts = (
        (workspace.root / "datasets", 2),
        (workspace.root / "canonical/v1", 1),
        (workspace.root / "accelerations/v1", 1),
    )
    for root, depth in layouts:
        if not _lexists(root):
            continue
        try:
            root_metadata = root.lstat()
            if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
                blockers.append(
                    f"managed storage root is unsafe: {root.relative_to(workspace.root)}"
                )
                continue
            current = [root]
            for _ in range(depth):
                next_level: list[Path] = []
                for parent in current:
                    child_count = 0
                    with os.scandir(parent) as entries:
                        for entry in sorted(entries, key=lambda item: item.name):
                            child_count += 1
                            child = Path(entry.path)
                            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                                blockers.append(
                                    "unexpected entry in managed final root: "
                                    f"{child.relative_to(workspace.root)}"
                                )
                            else:
                                next_level.append(child)
                    if child_count == 0 and parent != root:
                        leaves.append(parent)
                current = next_level
            leaves.extend(current)
        except OSError as error:
            blockers.append(f"cannot inspect managed final root {root}: {error}")
    return leaves, blockers


def _prune_empty_dataset_containers(workspace: Workspace) -> None:
    root = workspace.root / "datasets"
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                with contextlib.suppress(OSError):
                    Path(entry.path).rmdir()
    fsync_directory(root)


def _inventory(workspace: Workspace) -> _Inventory:
    items: list[GarbageCollectionItem] = []
    blockers: list[str] = []
    filesystem_paths: set[str] = set()
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        operations = list(
            connection.execute("SELECT id, status FROM operations ORDER BY started_at, id")
        )
        datasets = list(connection.execute("SELECT * FROM datasets ORDER BY id"))
        versions = list(connection.execute("SELECT * FROM dataset_versions ORDER BY id"))
        source_blobs = list(connection.execute("SELECT * FROM source_blobs ORDER BY sha256"))
        artifacts = list(connection.execute("SELECT * FROM artifacts ORDER BY id"))
        canonical_profiles = list(
            connection.execute("SELECT * FROM canonical_profiles ORDER BY id")
        )
        index_profiles = list(connection.execute("SELECT * FROM index_profiles ORDER BY id"))

    for operation in operations:
        if operation["status"] not in _TERMINAL_OPERATION_STATUSES:
            blockers.append(
                f"operation {operation['id']} is still {operation['status']}; run recover first"
            )

    version_by_id = {str(row["id"]): row for row in versions}
    canonical_by_id = {str(row["id"]): row for row in canonical_profiles}
    index_by_id = {str(row["id"]): row for row in index_profiles}
    published_versions = {
        str(row["id"]) for row in versions if row["status"] == OperationStatus.PUBLISHED.value
    }
    published_canonical = {
        str(row["id"])
        for row in canonical_profiles
        if row["status"] == OperationStatus.PUBLISHED.value
    }
    published_indexes = {
        str(row["id"]) for row in index_profiles if row["status"] == OperationStatus.PUBLISHED.value
    }

    for row in datasets:
        dataset_id = str(row["id"])
        active_version = row["active_version_id"]
        active_canonical = row["active_canonical_profile_id"]
        active_index = row["active_index_profile_id"]
        if active_version is not None and str(active_version) not in published_versions:
            blockers.append(f"dataset {dataset_id} points to a non-published active version")
        if active_canonical is not None and str(active_canonical) not in published_canonical:
            blockers.append(f"dataset {dataset_id} points to a non-published canonical profile")
        if active_index is not None and str(active_index) not in published_indexes:
            blockers.append(f"dataset {dataset_id} points to a non-published index profile")

    for profile_id in published_canonical:
        if str(canonical_by_id[profile_id]["dataset_version_id"]) not in published_versions:
            blockers.append(f"published canonical profile {profile_id} has no published version")
    for profile_id in published_indexes:
        row = index_by_id[profile_id]
        if str(row["dataset_version_id"]) not in published_versions:
            blockers.append(f"published index profile {profile_id} has no published version")
        if str(row["canonical_profile_id"]) not in published_canonical:
            blockers.append(
                f"published index profile {profile_id} has no published canonical profile"
            )

    candidate_version_ids = {
        str(row["id"]) for row in versions if str(row["id"]) not in published_versions
    }
    candidate_canonical_ids = {
        str(row["id"]) for row in canonical_profiles if str(row["id"]) not in published_canonical
    }
    candidate_index_ids = {
        str(row["id"])
        for row in index_profiles
        if str(row["id"]) not in published_indexes and str(row["status"]) != "RETIRED"
    }

    for row in versions:
        parent_id = row["parent_version_id"]
        if str(row["id"]) in published_versions and parent_id in candidate_version_ids:
            blockers.append(
                f"published dataset version {row['id']} references abandoned parent {parent_id}"
            )
    for row in canonical_profiles:
        parent_id = row["parent_profile_id"]
        if str(row["id"]) in published_canonical and parent_id in candidate_canonical_ids:
            blockers.append(
                f"published canonical profile {row['id']} references abandoned parent {parent_id}"
            )
    # Acceleration profile ancestry is an audit chain. An active profile may point to
    # a superseded profile whose disposable artifacts are ready for collection.

    candidate_artifact_ids: set[str] = set()
    candidate_artifact_paths: set[str] = set()
    protected_artifact_paths: set[str] = set()
    registered_paths: set[str] = set()
    for row in artifacts:
        artifact_id = str(row["id"])
        relative_text = str(row["relative_path"])
        try:
            relative = _relative_path(relative_text, roots=_FINAL_ROOTS)
        except WorkspaceError as error:
            blockers.append(f"artifact {artifact_id}: {error}")
            continue
        registered_paths.add(str(relative))
        version_id = str(row["dataset_version_id"])
        canonical_id = row["canonical_profile_id"]
        index_id = row["index_profile_id"]
        kind = str(row["kind"])
        expected_root = {
            "RECORD_PARQUET": PurePosixPath("datasets"),
            "CSV_REJECTS": PurePosixPath("datasets"),
            "NORMALIZED_PARQUET": PurePosixPath("datasets"),
            "CANONICAL_PARQUET": PurePosixPath("canonical/v1"),
            "VALUE_ACCELERATION": PurePosixPath("accelerations/v1"),
            "TOKEN_ACCELERATION": PurePosixPath("accelerations/v1"),
        }.get(kind)
        if expected_root is None or not relative.is_relative_to(expected_root):
            blockers.append(f"artifact {artifact_id} has an unexpected kind/path layout")
        protected = False
        if version_id not in published_versions:
            protected = False
        elif kind in {"RECORD_PARQUET", "NORMALIZED_PARQUET", "CSV_REJECTS"}:
            protected = True
            if canonical_id is not None or index_id is not None:
                blockers.append(
                    f"published record artifact {artifact_id} has unexpected profile links"
                )
        elif kind == "CANONICAL_PARQUET" and canonical_id is not None:
            protected = str(canonical_id) in published_canonical
        elif kind in {"VALUE_ACCELERATION", "TOKEN_ACCELERATION"} and index_id is not None:
            protected = str(index_id) in published_indexes
        else:
            protected = True
            blockers.append(f"published artifact {artifact_id} has ambiguous ownership")
        if protected:
            protected_artifact_paths.add(str(relative))
            path = workspace.root / relative
            try:
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise OSError("not a regular file")
                if metadata.st_size != int(row["size_bytes"]):
                    blockers.append(f"published artifact size mismatch: {relative}")
            except OSError:
                blockers.append(f"published artifact is missing or unsafe: {relative}")
        else:
            candidate_artifact_ids.add(artifact_id)
            candidate_artifact_paths.add(str(relative))

    published_source_sha256s = {
        str(version_by_id[version_id]["source_sha256"]) for version_id in published_versions
    }
    source_by_sha = {str(row["sha256"]): row for row in source_blobs}
    source_paths: dict[str, str] = {}
    for sha256, row in source_by_sha.items():
        relative_text = str(row["stored_path"])
        try:
            relative = _relative_path(relative_text, roots=(PurePosixPath("objects/raw/sha256"),))
            if relative.name != sha256 or len(relative.parts) != 4:
                raise WorkspaceError("RAW path does not match its content address")
            source_paths[sha256] = str(relative)
        except WorkspaceError as error:
            blockers.append(f"source blob {sha256}: {error}")
    for sha256 in sorted(published_source_sha256s):
        row = source_by_sha.get(sha256)
        if row is None:
            blockers.append(f"published dataset version references missing RAW metadata: {sha256}")
            continue
        relative_text = source_paths.get(sha256, str(row["stored_path"]))
        try:
            relative = _relative_path(relative_text, roots=(PurePosixPath("objects/raw/sha256"),))
            path = workspace.root / relative
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise OSError("not a regular file")
            if metadata.st_size != int(row["size_bytes"]):
                blockers.append(f"published RAW size mismatch: {relative}")
        except (OSError, WorkspaceError):
            blockers.append(f"published RAW is missing or unsafe: {relative_text}")

    candidate_source_sha256s = set(source_by_sha) - published_source_sha256s
    published_source_paths = {
        source_paths[sha256] for sha256 in published_source_sha256s if sha256 in source_paths
    }
    candidate_dataset_ids = {
        str(row["id"])
        for row in datasets
        if not any(
            str(version["dataset_id"]) == str(row["id"])
            and str(version["id"]) in published_versions
            for version in versions
        )
        and all(
            value is None
            for value in (
                row["active_version_id"],
                row["active_canonical_profile_id"],
                row["active_index_profile_id"],
            )
        )
    }

    control_roots = (
        (workspace.root / "operations/staging", "staging", "abandoned staging data"),
        (workspace.root / "operations/orphans", "quarantine", "recovered operation quarantine"),
    )
    for root, kind, reason in control_roots:
        try:
            metadata = root.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                blockers.append(
                    f"managed operation root is unsafe: {root.relative_to(workspace.root)}"
                )
                continue
            with os.scandir(root) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    path = Path(entry.path)
                    relative = str(path.relative_to(workspace.root))
                    try:
                        size = _tree_size(path)
                    except (OSError, WorkspaceError) as error:
                        blockers.append(f"unsafe abandoned operation path {relative}: {error}")
                        continue
                    filesystem_paths.add(relative)
                    items.append(GarbageCollectionItem(kind, reason, relative, size_bytes=size))
        except OSError as error:
            blockers.append(f"cannot inspect managed operation root {root}: {error}")

    raw_root = workspace.root / "objects/raw/sha256"
    try:
        metadata = raw_root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            blockers.append("managed RAW root is unsafe")
        else:
            with os.scandir(raw_root) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    path = Path(entry.path)
                    relative = str(path.relative_to(workspace.root))
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        blockers.append(f"unexpected entry in managed RAW root: {relative}")
                        continue
                    if relative in published_source_paths:
                        continue
                    filesystem_paths.add(relative)
                    reason = (
                        "RAW is referenced only by unpublished versions"
                        if entry.name in source_by_sha
                        else "RAW has no catalog source_blob"
                    )
                    items.append(
                        GarbageCollectionItem(
                            "raw", reason, relative, entry.name, entry.stat().st_size
                        )
                    )
    except OSError as error:
        blockers.append(f"cannot inspect managed RAW root: {error}")

    leaves, leaf_blockers = _managed_final_leaves(workspace)
    blockers.extend(leaf_blockers)
    protected_absolute = {str(workspace.root / path) for path in protected_artifact_paths}
    candidate_absolute = {str(workspace.root / path) for path in candidate_artifact_paths}
    registered_absolute = {str(workspace.root / path) for path in registered_paths}
    for leaf in sorted(leaves):
        relative = str(leaf.relative_to(workspace.root))
        try:
            size = _tree_size(leaf)
            files = _files_below(leaf)
        except (OSError, WorkspaceError) as error:
            blockers.append(f"unsafe final artifact directory {relative}: {error}")
            continue
        protected_here = {path for path in protected_absolute if path.startswith(f"{leaf}{os.sep}")}
        if protected_here:
            if any(path.startswith(f"{leaf}{os.sep}") for path in candidate_absolute):
                blockers.append(
                    f"published and abandoned artifacts share one directory: {relative}"
                )
            unexpected = files - registered_absolute
            if unexpected:
                blockers.append(
                    f"published artifact directory contains unregistered files: {relative}"
                )
            continue
        filesystem_paths.add(relative)
        reason = (
            "final artifact directory is referenced only by unpublished catalog state"
            if any(path.startswith(f"{leaf}{os.sep}") for path in registered_absolute)
            else "final artifact directory is invisible to the catalog"
        )
        items.append(GarbageCollectionItem("final_artifact", reason, relative, size_bytes=size))

    for artifact_id in sorted(candidate_artifact_ids):
        items.append(
            GarbageCollectionItem(
                "artifact_row", "artifact belongs to unpublished state", catalog_id=artifact_id
            )
        )
    for profile_id in sorted(candidate_index_ids):
        items.append(
            GarbageCollectionItem(
                "index_profile", "index profile is not published", catalog_id=profile_id
            )
        )
    for profile_id in sorted(candidate_canonical_ids):
        items.append(
            GarbageCollectionItem(
                "canonical_profile", "canonical profile is not published", catalog_id=profile_id
            )
        )
    for version_id in sorted(candidate_version_ids):
        items.append(
            GarbageCollectionItem(
                "dataset_version", "dataset version is not published", catalog_id=version_id
            )
        )
    for sha256 in sorted(candidate_source_sha256s):
        items.append(
            GarbageCollectionItem(
                "source_blob", "RAW metadata has no published version", catalog_id=sha256
            )
        )
    for dataset_id in sorted(candidate_dataset_ids):
        items.append(
            GarbageCollectionItem(
                "dataset", "dataset has no published versions", catalog_id=dataset_id
            )
        )

    ordered_items = tuple(
        sorted(
            items,
            key=lambda item: (
                item.kind,
                item.relative_path or "",
                item.catalog_id or "",
                item.reason,
            ),
        )
    )
    return _Inventory(
        GarbageCollectionPlan(tuple(sorted(set(blockers))), ordered_items),
        tuple(sorted(filesystem_paths)),
        tuple(sorted(candidate_artifact_ids)),
        tuple(sorted(candidate_index_ids)),
        tuple(sorted(candidate_canonical_ids)),
        tuple(sorted(candidate_version_ids)),
        tuple(sorted(candidate_source_sha256s)),
        tuple(sorted(candidate_dataset_ids)),
    )


def recover(workspace: Workspace) -> list[dict[str, Any]]:
    recovered: list[dict[str, Any]] = []
    with workspace.lock(exclusive=True):
        staging = workspace.root / "operations/staging"
        orphan_root = workspace.root / "operations/orphans"
        for root in (staging, orphan_root):
            metadata = root.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise WorkspaceError(f"managed operation root is unsafe: {root}")
        candidates = sorted(staging.iterdir(), key=lambda path: path.name)
        for candidate in candidates:
            if candidate.is_symlink():
                raise WorkspaceError(
                    f"refusing to recover symlinked staging path: {candidate.name}"
                )

        with workspace.catalog.transaction() as connection:
            operations = list(
                connection.execute(
                    """
                    SELECT id, status FROM operations
                    WHERE status NOT IN ('PUBLISHED', 'FAILED', 'ABORTED')
                    ORDER BY started_at
                    """
                )
            )
            now = utc_now()
            for operation in operations:
                connection.execute(
                    """
                    UPDATE operations
                    SET status = ?, updated_at = ?, error = ?
                    WHERE id = ?
                    """,
                    (
                        OperationStatus.ABORTED.value,
                        now,
                        "recovered after interrupted operation",
                        operation["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE dataset_versions SET status = ?
                    WHERE id = (
                        SELECT dataset_version_id FROM operations WHERE id = ?
                    ) AND status != ?
                    """,
                    (
                        OperationStatus.ABORTED.value,
                        operation["id"],
                        OperationStatus.PUBLISHED.value,
                    ),
                )
            connection.execute(
                """
                UPDATE canonical_profiles SET status='FAILED'
                WHERE status != 'PUBLISHED'
                  AND id NOT IN (
                      SELECT active_canonical_profile_id FROM datasets
                      WHERE active_canonical_profile_id IS NOT NULL
                  )
                """
            )
            connection.execute(
                """
                UPDATE index_profiles SET status='FAILED'
                WHERE status != 'PUBLISHED'
                  AND id NOT IN (
                      SELECT active_index_profile_id FROM datasets
                      WHERE active_index_profile_id IS NOT NULL
                  )
                """
            )
            trigger_fault("recover.before_catalog_commit")
        trigger_fault("recover.after_catalog_commit")

        for candidate in candidates:
            target = orphan_root / candidate.name
            suffix = 0
            while _lexists(target):
                suffix += 1
                target = orphan_root / f"{candidate.name}-{suffix}"
            os.replace(candidate, target)
            recovered.append({"operation_id": candidate.name, "quarantined_path": str(target)})
        trigger_fault("recover.after_quarantine")
        fsync_directory(staging)
        fsync_directory(orphan_root)
    return recovered


def plan_gc(workspace: Workspace) -> GarbageCollectionPlan:
    """Return a deterministic, non-mutating reclamation plan."""
    with workspace.lock(exclusive=False):
        return _inventory(workspace).plan


def _delete_many(
    connection: sqlite3.Connection, table: str, column: str, values: tuple[str, ...]
) -> None:
    if not values:
        return
    placeholders = ",".join("?" for _ in values)
    connection.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", values)


def apply_gc(workspace: Workspace) -> GarbageCollectionResult:
    """Re-plan and reclaim abandoned state under the exclusive workspace lock."""
    with workspace.lock(exclusive=True):
        inventory = _inventory(workspace)
        if inventory.plan.blockers:
            raise WorkspaceError(
                "garbage collection is blocked: " + "; ".join(inventory.plan.blockers)
            )
        if not inventory.plan.items:
            return GarbageCollectionResult(None, (), ())

        operation_id = str(uuid.uuid4())
        now = utc_now()
        with workspace.catalog.transaction() as connection:
            connection.execute(
                """
                INSERT INTO operations(id, kind, status, started_at, updated_at)
                VALUES (?, 'GC', 'PLANNED', ?, ?)
                """,
                (operation_id, now, now),
            )
        trigger_fault("gc.operation_cataloged")

        stage = workspace.root / "operations/staging" / operation_id
        trash = stage / "trash"
        trash.mkdir(parents=True, mode=0o700)
        fsync_directory(stage.parent)
        try:
            for relative_text in inventory.filesystem_paths:
                relative = _relative_path(
                    relative_text,
                    roots=(
                        PurePosixPath("operations/staging"),
                        PurePosixPath("operations/orphans"),
                        PurePosixPath("objects/raw/sha256"),
                        *_FINAL_ROOTS,
                    ),
                )
                source = workspace.root / relative
                if not _lexists(source):
                    raise WorkspaceError(f"garbage candidate changed before apply: {relative}")
                if source.is_symlink():
                    raise WorkspaceError(f"garbage candidate became a symlink: {relative}")
                target = trash / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(source, target)
                fsync_directory(source.parent)
                fsync_directory(target.parent)
                trigger_fault("gc.after_filesystem_move")
            _prune_empty_dataset_containers(workspace)
            trigger_fault("gc.after_filesystem_moves")

            trigger_fault("gc.before_catalog_commit")
            with workspace.catalog.transaction() as connection:
                _delete_many(connection, "artifacts", "id", inventory.artifact_ids)
                if inventory.index_profile_ids:
                    connection.executemany(
                        "UPDATE index_profiles SET status='RETIRED' WHERE id=?",
                        ((item,) for item in inventory.index_profile_ids),
                    )
                _delete_many(
                    connection, "canonical_profiles", "id", inventory.canonical_profile_ids
                )
                _delete_many(connection, "dataset_versions", "id", inventory.dataset_version_ids)
                _delete_many(connection, "source_blobs", "sha256", inventory.source_sha256s)
                _delete_many(connection, "datasets", "id", inventory.dataset_ids)
                connection.execute(
                    "UPDATE operations SET status='PUBLISHED', updated_at=? WHERE id=?",
                    (utc_now(), operation_id),
                )
            trigger_fault("gc.after_catalog_commit")
        except BaseException as error:
            with contextlib.suppress(Exception), workspace.catalog.transaction() as connection:
                connection.execute(
                    """
                    UPDATE operations SET status='FAILED', updated_at=?, error=?
                    WHERE id=? AND status != 'PUBLISHED'
                    """,
                    (utc_now(), str(error)[:1000], operation_id),
                )
            raise

        shutil.rmtree(trash)
        fsync_directory(stage)
        trigger_fault("gc.after_trash_delete")
        with contextlib.suppress(OSError):
            stage.rmdir()
        fsync_directory(stage.parent)
        return GarbageCollectionResult(operation_id, (), inventory.plan.items)


def list_operations(workspace: Workspace) -> list[dict[str, Any]]:
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, kind, status, dataset_id, dataset_version_id,
                       started_at, updated_at, error
                FROM operations ORDER BY started_at DESC
                """
            )
        ]


def verify_workspace(workspace: Workspace) -> dict[str, Any]:
    """Verify immutable content against catalog metadata."""
    checked_raw = 0
    checked_artifacts = 0
    checked_bytes = 0
    with workspace.lock(exclusive=False):
        with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
            raw_rows = list(
                connection.execute("SELECT sha256, size_bytes, stored_path FROM source_blobs")
            )
            artifacts = list(
                connection.execute(
                    """
                    SELECT a.kind, a.format_version, a.relative_path, a.sha256,
                           a.size_bytes, a.dataset_version_id,
                           a.canonical_profile_id, a.index_profile_id, v.row_count,
                           ip.canonical_profile_id AS expected_canonical_profile_id,
                           ip.fingerprint AS expected_policy_fingerprint
                    FROM artifacts a
                    JOIN dataset_versions v ON v.id = a.dataset_version_id
                    LEFT JOIN index_profiles ip ON ip.id = a.index_profile_id
                    ORDER BY a.relative_path
                    """
                )
            )

        for row in raw_rows:
            path = workspace.resolve_relative(row["stored_path"])
            if path.is_symlink() or not path.is_file():
                raise WorkspaceError(f"RAW blob is missing or unsafe: {row['stored_path']}")
            digest, size = hash_file(path)
            if digest != row["sha256"] or size != row["size_bytes"]:
                raise WorkspaceError(f"RAW blob verification failed: {row['stored_path']}")
            checked_raw += 1
            checked_bytes += size

        for row in artifacts:
            path = workspace.resolve_relative(row["relative_path"])
            if path.is_symlink() or not path.is_file():
                raise WorkspaceError(f"artifact is missing or unsafe: {row['relative_path']}")
            digest, size = hash_file(path)
            if digest != row["sha256"] or size != row["size_bytes"]:
                raise WorkspaceError(f"artifact checksum failed: {row['relative_path']}")
            if row["kind"] in {"NORMALIZED_PARQUET", "RECORD_PARQUET", "CANONICAL_PARQUET"}:
                if pq.read_metadata(path).num_rows != row["row_count"]:
                    raise WorkspaceError(f"Parquet row count failed: {row['relative_path']}")
            elif row["kind"] in {"VALUE_ACCELERATION", "TOKEN_ACCELERATION"}:
                primitive = row["kind"].removesuffix("_ACCELERATION")
                try:
                    verify_acceleration(
                        path,
                        primitive=AccelerationKind(primitive),
                        dataset_version_id=str(row["dataset_version_id"]),
                        canonical_profile_id=str(row["expected_canonical_profile_id"]),
                        index_profile_id=str(row["index_profile_id"]),
                        policy_fingerprint=str(row["expected_policy_fingerprint"]),
                    )
                except RuntimeError as error:
                    raise WorkspaceError(
                        f"acceleration verification failed: {row['relative_path']}"
                    ) from error
            checked_artifacts += 1
            checked_bytes += size
    return {
        "raw_blobs": checked_raw,
        "artifacts": checked_artifacts,
        "bytes_verified": checked_bytes,
    }
