from __future__ import annotations

import contextlib
import json
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pyarrow.parquet as pq

from pandoracle.acceleration_profiles import (
    ACCELERATION_POLICY_VERSION,
    policy_fingerprint,
    policy_json,
)
from pandoracle.errors import ImportFailure
from pandoracle.faults import trigger_fault
from pandoracle.fs import fsync_directory, hash_file, utc_now
from pandoracle.ingest import (
    MINIMUM_RESERVE,
    ImportProgress,
    ImportResult,
    MalformedRecordPolicy,
    _build_artifacts,
    _read_csv_plan,
    _validate_reject_artifact,
)
from pandoracle.models import FieldAccelerationPolicy, OperationStatus
from pandoracle.normalize import NORMALIZER_VERSION, normalizer_id
from pandoracle.schema import (
    SchemaPlan,
    build_layer_specs,
    canonical_fingerprint,
    semantic_fingerprint,
)
from pandoracle.workspace import Workspace


def _active_source(workspace: Workspace, identifier: str) -> dict[str, Any]:
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        row = connection.execute(
            """
            SELECT d.id AS dataset_id, d.name AS dataset_name,
                   v.id AS version_id, v.source_sha256, v.source_name,
                   v.recipe_json, v.schema_fingerprint,
                   v.schema_json,
                   v.row_count, s.size_bytes, s.stored_path,
                   d.active_canonical_profile_id, d.active_index_profile_id,
                   cp.fingerprint AS canonical_fingerprint,
                   ip.policy_json AS index_policy_json
            FROM datasets d
            JOIN dataset_versions v ON v.id = d.active_version_id
            JOIN source_blobs s ON s.sha256 = v.source_sha256
            JOIN canonical_profiles cp ON cp.id = d.active_canonical_profile_id
            JOIN index_profiles ip ON ip.id = d.active_index_profile_id
            WHERE d.id = ? OR d.name = ?
            """,
            (identifier, identifier),
        ).fetchone()
    if row is None:
        raise ImportFailure(f"dataset not found or has no active version: {identifier}")
    return dict(row)


def analyze_dataset(workspace: Workspace, identifier: str) -> SchemaPlan:
    source = _active_source(workspace, identifier)
    recipe = json.loads(source["recipe_json"])
    raw = workspace.resolve_relative(source["stored_path"])
    draft = _read_csv_plan(raw, str(recipe.get("encoding", "utf-8")))[3]
    current = {
        int(field["field_id"]): str(field["semantic_type"])
        for field in json.loads(source["schema_json"])
    }
    active_custom = workspace.catalog.list_custom_types(include_retired=False)
    all_custom = workspace.catalog.list_custom_types()
    retired = set(all_custom) - set(active_custom)
    fields = []
    for field in draft.fields:
        selected = current[field.field_id]
        if selected in retired:
            field = replace(
                field,
                selected_type=selected,
                selection_source="previous_active",
                canonicalizer_id=normalizer_id(selected),
                canonicalizer_version=NORMALIZER_VERSION,
            )
        fields.append(field)
    return replace(draft, fields=tuple(fields))


def revise_dataset(
    workspace: Workspace,
    identifier: str,
    schema_plan: SchemaPlan,
    *,
    progress: Callable[[ImportProgress], None] | None = None,
) -> ImportResult:
    if not schema_plan.confirmed:
        raise ImportFailure("schema plan is not fully confirmed")
    with workspace.lock(exclusive=True):
        source = _active_source(workspace, identifier)
        raw_path = workspace.resolve_relative(source["stored_path"])
        source_size = int(source["size_bytes"])
        reserve = max(MINIMUM_RESERVE, int(source_size * 0.2))
        required = source_size * 2 + reserve
        free = shutil.disk_usage(workspace.root).free
        if free < required:
            raise ImportFailure(
                f"insufficient workspace space for revision: need {required} bytes, have {free}"
            )

        recipe_before = json.loads(source["recipe_json"])
        encoding = str(recipe_before.get("encoding", "utf-8"))
        dialect, headers, _sample, _draft, recipe = _read_csv_plan(raw_path, encoding)
        custom_types = workspace.catalog.list_custom_types()
        active_custom = workspace.catalog.list_custom_types(include_retired=False)
        retired = set(custom_types) - set(active_custom)
        previous = {
            int(field["field_id"]): str(field["semantic_type"])
            for field in json.loads(source["schema_json"])
        }
        newly_assigned = sorted(
            {
                str(field.selected_type)
                for field in schema_plan.fields
                if str(field.selected_type) in retired
                and previous.get(field.field_id) != str(field.selected_type)
            }
            | ({item.type_id for item in schema_plan.custom_types} & retired)
        )
        if newly_assigned:
            raise ImportFailure(
                "custom semantic type is retired; restore before assigning with: "
                + "; ".join(
                    f"pandoracle types restore {item}" for item in newly_assigned
                )
            )
        semantic_fields, canonical_fields = build_layer_specs(
            schema_plan, headers, recipe, custom_types
        )
        error_policy = MalformedRecordPolicy(
            recipe_before.get("malformed_record_policy", MalformedRecordPolicy.ABORT)
        )
        effective_recipe = {**recipe, "malformed_record_policy": error_policy.value}
        empty_accelerations = tuple(
            FieldAccelerationPolicy(field.field_id, field.semantic_type, ())
            for field in canonical_fields
        )
        fingerprint = semantic_fingerprint(semantic_fields, effective_recipe)
        operation_id = str(uuid.uuid4())
        if fingerprint == source.get("schema_fingerprint"):
            now = utc_now()
            with workspace.catalog.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO operations(
                        id, kind, status, dataset_id, dataset_version_id,
                        started_at, updated_at
                    ) VALUES (?, 'SCHEMA_REVISION', 'PUBLISHED', ?, ?, ?, ?)
                    """,
                    (operation_id, source["dataset_id"], source["version_id"], now, now),
                )
                row = connection.execute(
                    """
                    SELECT row_count, schema_json, normalization_stats_json
                    FROM dataset_versions WHERE id = ?
                    """,
                    (source["version_id"],),
                ).fetchone()
            return ImportResult(
                operation_id=operation_id,
                dataset_id=str(source["dataset_id"]),
                dataset_name=str(source["dataset_name"]),
                dataset_version_id=str(source["version_id"]),
                source_sha256=str(source["source_sha256"]),
                source_size_bytes=source_size,
                row_count=int(row["row_count"]),
                posting_count=0,
                deduplicated=True,
                fields=json.loads(row["schema_json"]),
                normalization_stats=json.loads(row["normalization_stats_json"] or "{}"),
                canonical_profile_id=str(source["active_canonical_profile_id"]),
                index_profile_id=str(source["active_index_profile_id"]),
                dataset_version_reused=True,
                canonical_profile_reused=True,
                index_profile_reused=True,
                quarantined_count=int(recipe_before.get("quarantined_record_count", 0)),
                quarantine_path=(
                    f"datasets/{source['dataset_id']}/{source['version_id']}/"
                    "rejects/rejected-records.jsonl"
                    if int(recipe_before.get("quarantined_record_count", 0))
                    else None
                ),
            )

        version_id = str(uuid.uuid4())
        canonical_profile_id = str(uuid.uuid4())
        index_profile_id = str(uuid.uuid4())
        canonical_profile_fingerprint = canonical_fingerprint(version_id, canonical_fields)
        index_profile_fingerprint = policy_fingerprint(canonical_profile_id, empty_accelerations)
        stage = workspace.root / "operations/staging" / operation_id
        stage.mkdir(mode=0o700)
        now = utc_now()
        with workspace.catalog.transaction() as connection:
            connection.execute(
                """
                INSERT INTO operations(
                    id, kind, status, dataset_id, dataset_version_id,
                    started_at, updated_at
                ) VALUES (?, 'SCHEMA_REVISION', ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    OperationStatus.ANALYZING.value,
                    source["dataset_id"],
                    version_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO dataset_versions(
                    id, dataset_id, source_sha256, source_name, status,
                    schema_json, recipe_json, created_at, parent_version_id,
                    schema_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    source["dataset_id"],
                    source["source_sha256"],
                    source["source_name"],
                    OperationStatus.ANALYZING.value,
                    json.dumps([item.to_dict() for item in semantic_fields], sort_keys=True),
                    json.dumps(effective_recipe, sort_keys=True),
                    now,
                    source["version_id"],
                    fingerprint,
                ),
            )
        trigger_fault("schema_revision.version_cataloged")

        try:
            workspace.catalog.set_operation_status(operation_id, OperationStatus.TRANSFORMING.value)
            expected_rows = int(source["row_count"])
            if progress is not None:
                progress(
                    ImportProgress(
                        OperationStatus.TRANSFORMING,
                        total=expected_rows,
                        unit="rows",
                    )
                )
            stage_dataset = stage / "dataset_version"
            stage_canonical = stage / "canonical_profile"
            built = _build_artifacts(
                raw_path,
                version_id,
                semantic_fields,
                canonical_fields,
                dialect,
                encoding,
                stage_dataset,
                stage_canonical,
                canonical_profile_id,
                progress,
                total_rows=expected_rows,
                on_error=error_policy,
            )
            row_count = built.accepted_count
            rejected_count = built.rejected_count
            record_path = built.record_path
            canonical_path = built.canonical_path
            reject_path = built.reject_path
            normalization_stats = built.normalization_stats
            posting_count = 0
            trigger_fault("schema_revision.artifacts_staged")
            workspace.catalog.set_operation_status(operation_id, OperationStatus.VALIDATING.value)
            if progress is not None:
                progress(ImportProgress(OperationStatus.VALIDATING))

            record_metadata = pq.read_metadata(record_path)
            canonical_metadata = pq.read_metadata(canonical_path)
            if record_metadata.num_rows != row_count or canonical_metadata.num_rows != row_count:
                raise ImportFailure("Parquet row count does not match parsed row count")
            if record_metadata.num_row_groups != canonical_metadata.num_row_groups:
                raise ImportFailure("record and canonical Parquet row groups are not aligned")
            record_sha256, record_size = hash_file(record_path)
            canonical_sha256, canonical_size = hash_file(canonical_path)
            if reject_path is not None:
                _validate_reject_artifact(reject_path, rejected_count)
                reject_digest_size = hash_file(reject_path)
            else:
                reject_digest_size = None
            trigger_fault("schema_revision.artifacts_validated")
            final_dataset_dir = workspace.root / "datasets" / str(source["dataset_id"]) / version_id
            final_canonical_dir = workspace.root / "canonical/v1" / canonical_profile_id
            final_dataset_dir.parent.mkdir(parents=True, exist_ok=True)
            final_canonical_dir.parent.mkdir(parents=True, exist_ok=True)
            if final_dataset_dir.exists() or final_canonical_dir.exists():
                raise ImportFailure("immutable artifact target already exists")
            os.replace(stage_dataset, final_dataset_dir)
            fsync_directory(final_dataset_dir.parent)
            trigger_fault("schema_revision.record_finalized")
            os.replace(stage_canonical, final_canonical_dir)
            fsync_directory(final_canonical_dir.parent)
            trigger_fault("schema_revision.canonical_finalized")

            record_relative = str(
                (final_dataset_dir / "data/part-00000.parquet").relative_to(workspace.root)
            )
            canonical_relative = str(
                (final_canonical_dir / "part-00000.parquet").relative_to(workspace.root)
            )
            reject_relative = (
                str(
                    (final_dataset_dir / "rejects/rejected-records.jsonl").relative_to(
                        workspace.root
                    )
                )
                if reject_path is not None
                else None
            )
            artifact_rows = [
                (
                    str(uuid.uuid4()),
                    version_id,
                    "RECORD_PARQUET",
                    1,
                    record_relative,
                    record_sha256,
                    record_size,
                    None,
                    None,
                ),
                (
                    str(uuid.uuid4()),
                    version_id,
                    "CANONICAL_PARQUET",
                    1,
                    canonical_relative,
                    canonical_sha256,
                    canonical_size,
                    canonical_profile_id,
                    None,
                ),
            ]
            if reject_relative is not None and reject_digest_size is not None:
                artifact_rows.append(
                    (
                        str(uuid.uuid4()),
                        version_id,
                        "CSV_REJECTS",
                        1,
                        reject_relative,
                        reject_digest_size[0],
                        reject_digest_size[1],
                        None,
                        None,
                    )
                )
            published_at = utc_now()
            trigger_fault("schema_revision.before_catalog_commit")
            with workspace.catalog.transaction() as connection:
                for custom_type in schema_plan.custom_types:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO custom_semantic_types(
                            type_id, label, normalizer_id, normalizer_version,
                            default_operator, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            custom_type.type_id,
                            custom_type.label,
                            custom_type.normalizer_id,
                            custom_type.normalizer_version,
                            custom_type.default_operator,
                            published_at,
                        ),
                    )
                connection.executemany(
                    """
                    INSERT INTO artifacts(
                        id, dataset_version_id, kind, format_version, relative_path,
                        sha256, size_bytes, created_at, canonical_profile_id, index_profile_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [(*row[:7], published_at, *row[7:]) for row in artifact_rows],
                )
                connection.execute(
                    """
                    INSERT INTO canonical_profiles(
                        id, dataset_version_id, parent_profile_id, status, fingerprint,
                        spec_json, statistics_json, created_at, published_at
                    ) VALUES (?, ?, ?, 'PUBLISHED', ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical_profile_id,
                        version_id,
                        source["active_canonical_profile_id"],
                        canonical_profile_fingerprint,
                        json.dumps([item.to_dict() for item in canonical_fields], sort_keys=True),
                        json.dumps(normalization_stats, sort_keys=True),
                        published_at,
                        published_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO index_profiles(
                        id, dataset_version_id, canonical_profile_id, parent_profile_id,
                        status, fingerprint, policy_json, policy_version, posting_count,
                        created_at, published_at
                    ) VALUES (?, ?, ?, ?, 'PUBLISHED', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        index_profile_id,
                        version_id,
                        canonical_profile_id,
                        source["active_index_profile_id"],
                        index_profile_fingerprint,
                        policy_json(empty_accelerations),
                        ACCELERATION_POLICY_VERSION,
                        0,
                        published_at,
                        published_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE dataset_versions
                    SET status = ?, row_count = ?, published_at = ?,
                        normalization_stats_json = ?, recipe_json = ?
                    WHERE id = ?
                    """,
                    (
                        OperationStatus.PUBLISHED.value,
                        row_count,
                        published_at,
                        json.dumps(normalization_stats, sort_keys=True),
                        json.dumps(
                            {
                                **effective_recipe,
                                "quarantined_record_count": rejected_count,
                            },
                            sort_keys=True,
                        ),
                        version_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE datasets SET active_version_id=?, active_canonical_profile_id=?,
                                        active_index_profile_id=? WHERE id=?
                    """,
                    (
                        version_id,
                        canonical_profile_id,
                        index_profile_id,
                        source["dataset_id"],
                    ),
                )
                connection.execute(
                    "UPDATE operations SET status = ?, updated_at = ? WHERE id = ?",
                    (OperationStatus.PUBLISHED.value, published_at, operation_id),
                )
            trigger_fault("schema_revision.after_catalog_commit")
            with contextlib.suppress(OSError):
                stage.rmdir()
            if progress is not None:
                progress(ImportProgress(OperationStatus.PUBLISHED, 1, 1, "version"))
            return ImportResult(
                operation_id=operation_id,
                dataset_id=str(source["dataset_id"]),
                dataset_name=str(source["dataset_name"]),
                dataset_version_id=version_id,
                source_sha256=str(source["source_sha256"]),
                source_size_bytes=source_size,
                row_count=row_count,
                posting_count=posting_count,
                deduplicated=False,
                fields=[field.to_dict() for field in semantic_fields],
                normalization_stats=normalization_stats,
                canonical_profile_id=canonical_profile_id,
                index_profile_id=index_profile_id,
                quarantined_count=rejected_count,
                quarantine_path=reject_relative,
            )
        except Exception as error:
            with (
                contextlib.suppress(Exception),
                workspace.catalog.transaction() as connection,
            ):
                connection.execute(
                    """
                    UPDATE operations SET status = ?, updated_at = ?, error = ?
                    WHERE id = ? AND status != ?
                    """,
                    (
                        OperationStatus.FAILED.value,
                        utc_now(),
                        str(error)[:1000],
                        operation_id,
                        OperationStatus.PUBLISHED.value,
                    ),
                )
                connection.execute(
                    """
                    UPDATE dataset_versions SET status = ?
                    WHERE id = ? AND status != ?
                    """,
                    (
                        OperationStatus.FAILED.value,
                        version_id,
                        OperationStatus.PUBLISHED.value,
                    ),
                )
            if isinstance(error, ImportFailure):
                raise
            raise ImportFailure(f"schema revision failed before publication: {error}") from error
