from __future__ import annotations

import contextlib
import json
from bisect import bisect_right
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq

from pandoracle.errors import SearchFailure
from pandoracle.models import (
    SearchRequest,
    SearchResult,
    SemanticFieldSpec,
)
from pandoracle.universal_search import execute_search
from pandoracle.workspace import Workspace


def _semantic_fields(value: str) -> list[SemanticFieldSpec]:
    return [SemanticFieldSpec.from_dict(item) for item in json.loads(value)]


def _field_record(
    parquet_file: Path,
    row_group_id: int,
    row_offset: int,
    fields: list[SemanticFieldSpec],
) -> tuple[dict[str, str | None], dict[int, str | None]]:
    parquet = pq.ParquetFile(parquet_file)
    if row_group_id < 0 or row_group_id >= parquet.num_row_groups:
        raise SearchFailure("record has an invalid Parquet row-group locator")
    table = parquet.read_row_group(row_group_id, columns=[field.storage_name for field in fields])
    if row_offset < 0 or row_offset >= table.num_rows:
        raise SearchFailure("record has an invalid row offset")
    row = pc.take(table, [row_offset]).to_pylist()[0]
    by_id = {field.field_id: row[field.storage_name] for field in fields}
    record: dict[str, str | None] = {}
    seen: set[str] = set()
    for field in fields:
        name = (
            field.source_name
            if field.source_name not in seen
            else f"{field.source_name}[{field.field_id}]"
        )
        seen.add(name)
        record[name] = by_id[field.field_id]
    return record, by_id


def inspect_record(
    workspace: Workspace, external_id: str, *, include_fields: bool = False
) -> dict[str, Any]:
    try:
        version_id, ordinal_text = external_id.rsplit(":", 1)
        ordinal = int(ordinal_text)
    except ValueError as error:
        raise SearchFailure("record reference must be <dataset-version-id>:<ordinal>") from error
    if ordinal < 0:
        raise SearchFailure("record ordinal cannot be negative")
    statement = """
        SELECT d.id AS dataset_id,d.name AS dataset_name,v.id AS dataset_version_id,
               v.source_name,v.source_sha256,v.schema_json,ap.relative_path AS parquet_path
        FROM dataset_versions v JOIN datasets d ON d.id=v.dataset_id
        JOIN artifacts ap ON ap.dataset_version_id=v.id
          AND ap.kind IN ('RECORD_PARQUET','NORMALIZED_PARQUET')
        WHERE v.id=? AND v.status='PUBLISHED'
    """
    with workspace.lock(exclusive=False):
        with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
            row = connection.execute(statement, (version_id,)).fetchone()
        if row is None:
            raise SearchFailure(f"published dataset version not found: {version_id}")
        segment = dict(row)
        fields = _semantic_fields(segment["schema_json"])
        parquet_path = workspace.resolve_relative(segment["parquet_path"])
        parquet = pq.ParquetFile(parquet_path)
        starts = [0]
        for group in range(parquet.num_row_groups):
            starts.append(starts[-1] + parquet.metadata.row_group(group).num_rows)
        group = bisect_right(starts, ordinal) - 1
        if group < 0 or group >= parquet.num_row_groups:
            raise SearchFailure(f"record ordinal {ordinal} is outside this dataset version")
        record, by_id = _field_record(parquet_path, group, ordinal - starts[group], fields)
    result: dict[str, Any] = {
        "record_ref": external_id,
        "dataset_id": segment["dataset_id"],
        "dataset_name": segment["dataset_name"],
        "dataset_version_id": version_id,
        "source_name": segment["source_name"],
        "source_sha256": segment["source_sha256"],
        "record": record,
    }
    if include_fields:
        result["fields"] = [
            {
                "field_id": field.field_id,
                "field_name": field.source_name,
                "semantic_type": field.semantic_type,
                "value": by_id[field.field_id],
            }
            for field in fields
        ]
    return result


def search(workspace: Workspace, request: SearchRequest) -> SearchResult:
    """Execute the v1 universal semantic search contract."""
    return execute_search(workspace, request)
