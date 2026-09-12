from __future__ import annotations

import contextlib
import csv
import heapq
import json
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

import pyarrow as pa
import pyarrow.parquet as pq

from pandoracle.acceleration_profiles import (
    ACCELERATION_POLICY_VERSION,
    policy_fingerprint,
    policy_json,
)
from pandoracle.errors import ImportFailure
from pandoracle.faults import trigger_fault
from pandoracle.fs import copy_and_hash, fsync_directory, fsync_file, hash_file, utc_now
from pandoracle.models import (
    CanonicalFieldSpec,
    FieldAccelerationPolicy,
    OperationStatus,
    SemanticFieldSpec,
)
from pandoracle.normalize import normalize_with
from pandoracle.schema import (
    SchemaPlan,
    build_layer_specs,
    canonical_fingerprint,
    load_schema_document,
    make_draft_plan,
    semantic_fingerprint,
)
from pandoracle.workspace import Workspace

BATCH_ROWS = 65_536
SAMPLE_BYTES = 256 * 1024
SAMPLE_ROWS = 512
MINIMUM_RESERVE = 256 * 1024 * 1024
REJECT_ESCALATION_MIN_RECORDS = 100
REJECT_ESCALATION_MIN_COUNT = 10
REJECT_ESCALATION_RATE = 0.10


class MalformedRecordPolicy(StrEnum):
    ABORT = "abort"
    QUARANTINE = "quarantine"


class MalformedRecordAction(StrEnum):
    ABORT = "abort"
    QUARANTINE = "quarantine"
    QUARANTINE_SIMILAR = "quarantine_similar"


@dataclass(frozen=True)
class MalformedRecord:
    source_record_number: int
    line_start: int
    line_end: int
    expected_fields: int
    actual_fields: int | None
    reason: str
    raw_record: str

    @property
    def signature(self) -> tuple[int, int | None, str]:
        return (self.expected_fields, self.actual_fields, self.reason)

    @property
    def position(self) -> str:
        lines = (
            f"line {self.line_start}"
            if self.line_start == self.line_end
            else f"lines {self.line_start}-{self.line_end}"
        )
        return f"source record {self.source_record_number} ({lines})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_record_number": self.source_record_number,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "expected_fields": self.expected_fields,
            "actual_fields": self.actual_fields,
            "reason": self.reason,
            "raw_record": self.raw_record,
        }


@dataclass(frozen=True)
class RejectEscalation:
    rejected_count: int
    processed_count: int

    @property
    def reject_rate(self) -> float:
        return self.rejected_count / self.processed_count


MalformedRecordHandler = Callable[[MalformedRecord], MalformedRecordAction | str]
RejectEscalationHandler = Callable[[RejectEscalation], bool]


class _TrackingLineIterator:
    """Expose physical lines to csv.reader while retaining the current raw record."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.line_number = 0
        self.current_lines: list[str] = []

    def __iter__(self) -> _TrackingLineIterator:
        return self

    def __next__(self) -> str:
        line = self.stream.readline()
        if line == "":
            raise StopIteration
        self.line_number += 1
        self.current_lines.append(line)
        return line

    def begin_record(self) -> int:
        self.current_lines = []
        return self.line_number + 1

    @property
    def raw_record(self) -> str:
        return "".join(self.current_lines)


def _validate_reject_artifact(path: Path, expected_count: int) -> None:
    count = 0
    previous_record_number = 0
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                record_number = int(value["source_record_number"])
                if record_number <= previous_record_number:
                    raise ValueError("source record positions are not increasing")
                if int(value["line_start"]) > int(value["line_end"]):
                    raise ValueError("physical line span is invalid")
                if not isinstance(value["raw_record"], str) or not isinstance(
                    value["reason"], str
                ):
                    raise ValueError("raw record or reason is invalid")
                previous_record_number = record_number
                count += 1
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ImportFailure(f"CSV reject artifact validation failed: {error}") from error
    if count != expected_count:
        raise ImportFailure(
            f"CSV reject artifact has {count} records; expected {expected_count}"
        )


@dataclass(frozen=True)
class ImportResult:
    operation_id: str
    dataset_id: str
    dataset_name: str
    dataset_version_id: str
    source_sha256: str
    source_size_bytes: int
    row_count: int
    posting_count: int
    deduplicated: bool
    fields: list[dict[str, Any]]
    normalization_stats: dict[str, dict[str, Any]]
    canonical_profile_id: str | None = None
    index_profile_id: str | None = None
    dataset_version_reused: bool = False
    canonical_profile_reused: bool = False
    index_profile_reused: bool = False
    quarantined_count: int = 0
    quarantine_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "dataset_version_id": self.dataset_version_id,
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "row_count": self.row_count,
            "accepted_count": self.row_count,
            "posting_count": self.posting_count,
            "deduplicated": self.deduplicated,
            "fields": self.fields,
            "normalization_stats": self.normalization_stats,
            "canonical_profile_id": self.canonical_profile_id,
            "index_profile_id": self.index_profile_id,
            "dataset_version_reused": self.dataset_version_reused,
            "canonical_profile_reused": self.canonical_profile_reused,
            "index_profile_reused": self.index_profile_reused,
            "quarantined_count": self.quarantined_count,
            "quarantine_path": self.quarantine_path,
        }


@dataclass(frozen=True)
class ImportProgress:
    phase: OperationStatus
    completed: int = 0
    total: int | None = None
    unit: str = ""


@dataclass(frozen=True)
class _ArtifactBuildResult:
    accepted_count: int
    rejected_count: int
    record_path: Path
    canonical_path: Path
    reject_path: Path | None
    normalization_stats: dict[str, dict[str, Any]]


def _safe_dataset_name(requested: str) -> str:
    name = requested.strip()
    if not name or len(name) > 128:
        raise ImportFailure("dataset name must contain between 1 and 128 characters")
    if any(character in name for character in ("/", "\\", "\x00")):
        raise ImportFailure("dataset name contains a forbidden path character")
    if any(ord(character) < 32 for character in name):
        raise ImportFailure("dataset name contains a control character")
    return name


def _preflight(workspace: Workspace, source: Path) -> int:
    if not source.is_file():
        raise ImportFailure(f"source is not a regular file: {source}")
    if source.is_relative_to(workspace.root):
        raise ImportFailure("source files inside the workspace cannot be imported")
    source_size = source.stat().st_size
    free = shutil.disk_usage(workspace.root).free
    reserve = max(MINIMUM_RESERVE, int(source_size * 0.2))
    conservative_required = source_size * 3 + reserve
    if free < conservative_required:
        raise ImportFailure(
            "insufficient workspace space: "
            f"need a conservative {conservative_required} bytes, have {free} bytes"
        )
    return source_size


def _read_csv_plan(
    raw_path: Path, encoding: str
) -> tuple[csv.Dialect, list[str], list[list[str]], SchemaPlan, dict[str, Any]]:
    try:
        with raw_path.open("r", encoding=encoding, errors="strict", newline="") as stream:
            sample = stream.read(SAMPLE_BYTES)
    except (OSError, UnicodeError) as error:
        raise ImportFailure(f"cannot decode source as {encoding}: {error}") from error
    if not sample:
        raise ImportFailure("source file is empty")
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        with raw_path.open("r", encoding=encoding, errors="strict", newline="") as stream:
            lines = _TrackingLineIterator(stream)
            reader = csv.reader(lines, dialect=dialect, strict=True)
            lines.begin_record()
            headers = next(reader)
            rows = []
            while len(rows) < SAMPLE_ROWS:
                lines.begin_record()
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except csv.Error:
                    # Schema inference is advisory. Malformed sample records are
                    # handled under the explicit import policy during the full pass.
                    reader = csv.reader(lines, dialect=dialect, strict=True)
                    continue
                if len(row) != len(headers):
                    continue
                rows.append(row)
    except (OSError, UnicodeError, csv.Error) as error:
        raise ImportFailure(f"cannot parse CSV sample: {error}") from error
    if not headers:
        raise ImportFailure("CSV header has no fields")
    recipe = {
        "format": "csv",
        "encoding": encoding,
        "delimiter": dialect.delimiter,
        "quotechar": dialect.quotechar,
        "escapechar": dialect.escapechar,
        "doublequote": dialect.doublequote,
        "strict": True,
        "batch_rows": BATCH_ROWS,
    }
    return dialect, headers, rows, make_draft_plan(headers, rows, recipe), recipe


def analyze_csv(source_path: str | Path, *, encoding: str = "utf-8") -> SchemaPlan:
    """Create an unconfirmed, value-free-on-disk schema proposal for a CSV."""
    try:
        source = Path(source_path).expanduser().resolve(strict=True)
    except OSError as error:
        raise ImportFailure(f"cannot access source file: {error}") from error
    if not source.is_file():
        raise ImportFailure(f"source is not a regular file: {source}")
    return _read_csv_plan(source, encoding)[3]


def _build_artifacts(
    raw_path: Path,
    dataset_version_id: str,
    semantic_fields: list[SemanticFieldSpec],
    canonical_fields: list[CanonicalFieldSpec],
    dialect: csv.Dialect,
    encoding: str,
    stage_dataset: Path,
    stage_canonical: Path,
    canonical_profile_id: str,
    progress: Callable[[ImportProgress], None] | None = None,
    *,
    total_rows: int | None = None,
    on_error: MalformedRecordPolicy | str | None = None,
    malformed_handler: MalformedRecordHandler | None = None,
    escalation_handler: RejectEscalationHandler | None = None,
) -> _ArtifactBuildResult:
    try:
        error_policy = MalformedRecordPolicy(on_error or MalformedRecordPolicy.ABORT)
    except ValueError:
        raise ImportFailure(f"unknown malformed-record policy: {on_error}") from None
    data_dir = stage_dataset / "data"
    data_dir.mkdir(parents=True)
    stage_canonical.mkdir(parents=True)
    record_path = data_dir / "part-00000.parquet"
    canonical_path = stage_canonical / "part-00000.parquet"

    record_schema = pa.schema(
        [pa.field("record_ordinal", pa.uint64(), nullable=False)]
        + [pa.field(field.storage_name, pa.string()) for field in semantic_fields]
    )
    record_schema = record_schema.with_metadata(
        {
            b"pandoracle.artifact": b"original-records/v1",
            b"pandoracle.dataset-version": dataset_version_id.encode("ascii"),
        }
    )
    canonical_schema = pa.schema(
        [pa.field("record_ordinal", pa.uint64(), nullable=False)]
        + [pa.field(field.canonical_storage_name, pa.string()) for field in canonical_fields]
    ).with_metadata(
        {
            b"pandoracle.artifact": b"canonical-values/v1",
            b"pandoracle.dataset-version": dataset_version_id.encode("ascii"),
            b"pandoracle.canonical-profile": canonical_profile_id.encode("ascii"),
        }
    )
    record_writer = pq.ParquetWriter(
        record_path,
        record_schema,
        compression="zstd",
        use_dictionary=True,
        write_statistics=True,
    )
    canonical_writer = pq.ParquetWriter(
        canonical_path,
        canonical_schema,
        compression="zstd",
        use_dictionary=True,
        write_statistics=True,
    )
    row_count = 0
    rejected_count = 0
    source_record_count = 0
    quarantine_similar: set[tuple[int, int | None, str]] = set()
    next_escalation_count = REJECT_ESCALATION_MIN_COUNT
    reject_path: Path | None = None
    reject_stream: TextIO | None = None
    canonical_by_id = {field.field_id: field for field in canonical_fields}
    normalization_stats = {
        str(field.field_id): {
            "source_name": next(
                item.source_name for item in semantic_fields if item.field_id == field.field_id
            ),
            "semantic_type": field.semantic_type,
            "non_empty": 0,
            "normalized": 0,
            "invalid": 0,
            "canonical_utf8_bytes": 0,
            "token_sample_count": 0,
            "token_sample_postings": 0,
            "token_sample_key_bytes": 0,
        }
        for field in canonical_fields
    }
    token_samples: dict[int, list[tuple[int, int, int]]] = {
        field.field_id: [] for field in canonical_fields if field.tokenizer_id is not None
    }

    def flush(rows: list[list[str]], start_ordinal: int) -> None:
        if not rows:
            return
        ordinals = list(range(start_ordinal, start_ordinal + len(rows)))
        record_columns: dict[str, list[Any]] = {
            "record_ordinal": ordinals,
        }
        canonical_columns: dict[str, list[Any]] = {
            "record_ordinal": list(range(start_ordinal, start_ordinal + len(rows)))
        }
        for field in semantic_fields:
            originals = [row[field.field_id] for row in rows]
            record_columns[field.storage_name] = originals
            canonical = canonical_by_id.get(field.field_id)
            if canonical is None:
                continue
            stats = normalization_stats[str(field.field_id)]
            stats["non_empty"] += sum(bool(value.strip()) for value in originals)
            normalized_values: list[str | None] = []
            for original, ordinal in zip(originals, ordinals, strict=True):
                normalized = normalize_with(
                    canonical.canonicalizer_id,
                    canonical.canonicalizer_version,
                    original,
                ).normalized
                normalized_values.append(normalized)
                if normalized is not None:
                    stats["normalized"] += 1
                    stats["canonical_utf8_bytes"] += len(normalized.encode("utf-8"))
                    if canonical.tokenizer_id is not None:
                        # Keep the 4,096 lowest deterministic ordinal hashes. A
                        # first-N sample was badly biased for naturally ordered
                        # datasets, while this reservoir remains bounded and only
                        # tokenizes values that enter or replace a sample slot.
                        score = (
                            (ordinal + 1) * 0x9E3779B97F4A7C15
                            ^ (canonical.field_id + 1) * 0xD1B54A32D192ED03
                        ) & 0xFFFFFFFFFFFFFFFF
                        sample = token_samples[canonical.field_id]
                        if len(sample) < 4096 or score < -sample[0][0]:
                            tokens = set(normalized.split())
                            item = (
                                -score,
                                len(tokens),
                                sum(len(token.encode("utf-8")) for token in tokens),
                            )
                            if len(sample) < 4096:
                                heapq.heappush(sample, item)
                            else:
                                heapq.heapreplace(sample, item)
                elif original.strip():
                    stats["invalid"] += 1
            canonical_columns[canonical.canonical_storage_name] = normalized_values
        record_table = pa.Table.from_pydict(record_columns, schema=record_schema)
        canonical_table = pa.Table.from_pydict(canonical_columns, schema=canonical_schema)
        record_writer.write_table(record_table, row_group_size=len(rows))
        canonical_writer.write_table(canonical_table, row_group_size=len(rows))
        if progress is not None:
            progress(
                ImportProgress(
                    OperationStatus.TRANSFORMING,
                    completed=start_ordinal + len(rows),
                    total=total_rows,
                    unit="rows",
                )
            )

    def reject(record: MalformedRecord) -> None:
        nonlocal reject_path, reject_stream, rejected_count, next_escalation_count
        if record.signature in quarantine_similar:
            action = MalformedRecordAction.QUARANTINE
        elif malformed_handler is not None:
            try:
                action = MalformedRecordAction(malformed_handler(record))
            except ValueError:
                raise ImportFailure("malformed-record handler returned an invalid action") from None
        elif error_policy is MalformedRecordPolicy.QUARANTINE:
            action = MalformedRecordAction.QUARANTINE
        else:
            action = MalformedRecordAction.ABORT

        if action is MalformedRecordAction.ABORT:
            actual = "unknown" if record.actual_fields is None else str(record.actual_fields)
            raise ImportFailure(
                f"malformed CSV {record.position}: expected {record.expected_fields} fields, "
                f"got {actual}; {record.reason}"
            )
        if action is MalformedRecordAction.QUARANTINE_SIMILAR:
            quarantine_similar.add(record.signature)

        if reject_stream is None:
            reject_path = stage_dataset / "rejects/rejected-records.jsonl"
            reject_path.parent.mkdir(parents=True)
            reject_stream = reject_path.open("w", encoding="utf-8", newline="\n")
        reject_stream.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        rejected_count += 1

        escalation = RejectEscalation(rejected_count, source_record_count)
        should_escalate = (
            source_record_count >= REJECT_ESCALATION_MIN_RECORDS
            and rejected_count >= next_escalation_count
            and escalation.reject_rate >= REJECT_ESCALATION_RATE
        )
        if not should_escalate:
            return
        if escalation_handler is None or not escalation_handler(escalation):
            raise ImportFailure(
                "malformed-record safety limit reached: "
                f"{rejected_count:,} of {source_record_count:,} records rejected "
                f"({escalation.reject_rate:.1%}); review delimiter, encoding, and parser settings"
            )
        next_escalation_count = rejected_count * 2

    try:
        with raw_path.open("r", encoding=encoding, errors="strict", newline="") as stream:
            lines = _TrackingLineIterator(stream)
            reader = csv.reader(lines, dialect=dialect, strict=True)
            lines.begin_record()
            next(reader)
            batch: list[list[str]] = []
            batch_start = 0
            while True:
                line_start = lines.begin_record()
                source_record_count += 1
                try:
                    row = next(reader)
                except StopIteration:
                    source_record_count -= 1
                    break
                except csv.Error as error:
                    reject(
                        MalformedRecord(
                            source_record_count,
                            line_start,
                            max(line_start, lines.line_number),
                            len(semantic_fields),
                            None,
                            f"CSV parse error: {error}",
                            lines.raw_record,
                        )
                    )
                    # A strict csv reader cannot be reused after a parse error.
                    # Resume at the next physical line it did not consume.
                    reader = csv.reader(lines, dialect=dialect, strict=True)
                    continue
                if len(row) != len(semantic_fields):
                    reject(
                        MalformedRecord(
                            source_record_count,
                            line_start,
                            max(line_start, lines.line_number),
                            len(semantic_fields),
                            len(row),
                            "field count mismatch",
                            lines.raw_record,
                        )
                    )
                    continue
                batch.append(row)
                row_count += 1
                if len(batch) >= BATCH_ROWS:
                    flush(batch, batch_start)
                    batch_start = row_count
                    batch = []
            flush(batch, batch_start)
        record_writer.close()
        canonical_writer.close()
        if reject_stream is not None:
            reject_stream.close()
    except (OSError, UnicodeError, csv.Error, pa.ArrowException) as error:
        record_writer.close()
        canonical_writer.close()
        if reject_stream is not None:
            reject_stream.close()
        if isinstance(error, ImportFailure):
            raise
        raise ImportFailure(f"CSV transformation failed: {error}") from error
    except Exception:
        record_writer.close()
        canonical_writer.close()
        if reject_stream is not None:
            reject_stream.close()
        raise

    fsync_file(record_path)
    fsync_file(canonical_path)
    fsync_directory(data_dir)
    fsync_directory(stage_dataset)
    fsync_directory(stage_canonical)
    if reject_path is not None:
        fsync_file(reject_path)
        fsync_directory(reject_path.parent)
    for field_id, stats in normalization_stats.items():
        sample = token_samples.get(int(field_id), [])
        stats["token_sample_count"] = len(sample)
        stats["token_sample_postings"] = sum(item[1] for item in sample)
        stats["token_sample_key_bytes"] = sum(item[2] for item in sample)
        sample_count = len(sample)
        if sample_count:
            scale = int(stats["normalized"]) / sample_count
            stats["estimated_token_postings"] = round(int(stats["token_sample_postings"]) * scale)
            stats["estimated_token_key_bytes"] = round(int(stats["token_sample_key_bytes"]) * scale)
    return _ArtifactBuildResult(
        row_count,
        rejected_count,
        record_path,
        canonical_path,
        reject_path,
        normalization_stats,
    )


def _import_csv_unlocked(
    workspace: Workspace,
    source_path: str | Path,
    *,
    dataset_name: str | None = None,
    encoding: str = "utf-8",
    schema_path: Path | None = None,
    schema_plan: SchemaPlan | None = None,
    progress: Callable[[ImportProgress], None] | None = None,
    on_error: MalformedRecordPolicy | str | None = None,
    malformed_handler: MalformedRecordHandler | None = None,
    escalation_handler: RejectEscalationHandler | None = None,
) -> ImportResult:
    if schema_path is None and schema_plan is None:
        raise ImportFailure("a fully confirmed schema is required before import")
    if schema_path is not None and schema_plan is not None:
        raise ImportFailure("pass either schema_path or schema_plan, not both")
    try:
        source = Path(source_path).expanduser().resolve(strict=True)
    except OSError as error:
        raise ImportFailure(f"cannot access source file: {error}") from error
    source_size_expected = _preflight(workspace, source)
    name = _safe_dataset_name(dataset_name or source.stem)
    operation_id = str(uuid.uuid4())
    dataset_version_id = str(uuid.uuid4())
    canonical_profile_id = str(uuid.uuid4())
    index_profile_id = str(uuid.uuid4())
    stage = workspace.root / "operations/staging" / operation_id
    stage.mkdir(mode=0o700)

    dataset_id: str
    with workspace.catalog.transaction() as connection:
        existing = connection.execute("SELECT id FROM datasets WHERE name = ?", (name,)).fetchone()
        if existing:
            dataset_id = str(existing[0])
        else:
            dataset_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO datasets(id, name, created_at) VALUES (?, ?, ?)",
                (dataset_id, name, utc_now()),
            )
        now = utc_now()
        connection.execute(
            """
            INSERT INTO operations(
                id, kind, status, dataset_id, dataset_version_id,
                started_at, updated_at
            ) VALUES (?, 'IMPORT', ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                OperationStatus.PLANNED.value,
                dataset_id,
                dataset_version_id,
                now,
                now,
            ),
        )
    trigger_fault("import.operation_cataloged")

    try:
        workspace.catalog.set_operation_status(operation_id, OperationStatus.COPYING_RAW.value)
        if progress is not None:
            progress(
                ImportProgress(
                    OperationStatus.COPYING_RAW,
                    total=source_size_expected,
                    unit="bytes",
                )
            )
        staged_raw = stage / "source.raw"
        source_sha256, source_size = copy_and_hash(
            source,
            staged_raw,
            progress=(
                lambda completed: (
                    progress(
                        ImportProgress(
                            OperationStatus.COPYING_RAW,
                            completed=completed,
                            total=source_size_expected,
                            unit="bytes",
                        )
                    )
                    if progress is not None
                    else None
                )
            ),
        )
        trigger_fault("import.raw_staged")
        raw_relative = f"objects/raw/sha256/{source_sha256}"
        raw_final = workspace.root / raw_relative
        if raw_final.exists():
            existing_sha256, existing_size = hash_file(raw_final)
            if existing_sha256 != source_sha256 or existing_size != source_size:
                raise ImportFailure("existing content-addressed RAW blob failed verification")
            staged_raw.unlink()
        else:
            os.replace(staged_raw, raw_final)
            fsync_directory(raw_final.parent)
        trigger_fault("import.raw_published")

        workspace.catalog.set_operation_status(operation_id, OperationStatus.ANALYZING.value)
        if progress is not None:
            progress(ImportProgress(OperationStatus.ANALYZING))
        dialect, headers, _sample, _draft, recipe = _read_csv_plan(raw_final, encoding)
        resolved_plan: SchemaPlan
        if schema_plan is not None:
            resolved_plan = schema_plan
        else:
            resolved_plan = load_schema_document(Path(schema_path))
        custom_types = workspace.catalog.list_custom_types(include_retired=False)
        semantic_fields, canonical_fields = build_layer_specs(
            resolved_plan, headers, recipe, custom_types
        )
        policy = (
            MalformedRecordPolicy.QUARANTINE
            if malformed_handler is not None and on_error is None
            else MalformedRecordPolicy(on_error or MalformedRecordPolicy.ABORT)
        )
        effective_recipe = {**recipe, "malformed_record_policy": policy.value}
        empty_accelerations = tuple(
            FieldAccelerationPolicy(field.field_id, field.semantic_type, ())
            for field in canonical_fields
        )
        fingerprint = semantic_fingerprint(semantic_fields, effective_recipe)
        canonical_profile_fingerprint = canonical_fingerprint(dataset_version_id, canonical_fields)
        index_profile_fingerprint = policy_fingerprint(canonical_profile_id, empty_accelerations)
        schema_json = json.dumps([field.to_dict() for field in semantic_fields], sort_keys=True)
        recipe_json = json.dumps(effective_recipe, sort_keys=True)

        with workspace.catalog.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO source_blobs(
                    sha256, size_bytes, stored_path, original_name, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (source_sha256, source_size, raw_relative, source.name, utc_now()),
            )
            duplicate = connection.execute(
                """
                SELECT v.id, v.row_count, v.schema_json, v.normalization_stats_json,
                       d.active_canonical_profile_id, d.active_index_profile_id,
                       ip.posting_count, cp.fingerprint AS canonical_fingerprint,
                       ip.policy_json, v.recipe_json
                FROM dataset_versions v JOIN datasets d ON d.id = v.dataset_id
                LEFT JOIN canonical_profiles cp ON cp.id = d.active_canonical_profile_id
                LEFT JOIN index_profiles ip ON ip.id = d.active_index_profile_id
                WHERE v.dataset_id = ? AND v.source_sha256 = ?
                  AND v.schema_fingerprint = ? AND v.status = 'PUBLISHED'
                  AND d.active_version_id = v.id
                ORDER BY v.published_at DESC LIMIT 1
                """,
                (dataset_id, source_sha256, fingerprint),
            ).fetchone()
        trigger_fault("import.source_cataloged")
        if duplicate:
            with workspace.catalog.transaction() as connection:
                connection.execute(
                    """
                    UPDATE operations SET status=?, dataset_version_id=?, updated_at=? WHERE id=?
                    """,
                    (OperationStatus.PUBLISHED.value, duplicate[0], utc_now(), operation_id),
                )
            # Publication is already durable. Recovery may quarantine a
            # harmless leftover directory; cleanup cannot undo success.
            with contextlib.suppress(OSError):
                stage.rmdir()
            fields = json.loads(str(duplicate[2]))
            duplicate_recipe = json.loads(str(duplicate[9]) or "{}")
            quarantined_count = int(duplicate_recipe.get("quarantined_record_count", 0))
            if progress is not None:
                progress(ImportProgress(OperationStatus.PUBLISHED, 1, 1, "version"))
            return ImportResult(
                operation_id=operation_id,
                dataset_id=dataset_id,
                dataset_name=name,
                dataset_version_id=str(duplicate[0]),
                source_sha256=source_sha256,
                source_size_bytes=source_size,
                row_count=int(duplicate[1]),
                posting_count=int(duplicate[6] or 0),
                deduplicated=True,
                fields=fields,
                normalization_stats=json.loads(str(duplicate[3]) or "{}"),
                canonical_profile_id=str(duplicate[4]),
                index_profile_id=str(duplicate[5]),
                dataset_version_reused=True,
                canonical_profile_reused=True,
                index_profile_reused=True,
                quarantined_count=quarantined_count,
                quarantine_path=(
                    f"datasets/{dataset_id}/{duplicate[0]}/rejects/rejected-records.jsonl"
                    if quarantined_count
                    else None
                ),
            )

        with workspace.catalog.transaction() as connection:
            connection.execute(
                """
                INSERT INTO dataset_versions(
                    id, dataset_id, source_sha256, source_name, status,
                    schema_json, recipe_json, created_at, schema_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_version_id,
                    dataset_id,
                    source_sha256,
                    source.name,
                    OperationStatus.ANALYZING.value,
                    schema_json,
                    recipe_json,
                    utc_now(),
                    fingerprint,
                ),
            )
        trigger_fault("import.version_cataloged")

        workspace.catalog.set_operation_status(operation_id, OperationStatus.TRANSFORMING.value)
        if progress is not None:
            progress(ImportProgress(OperationStatus.TRANSFORMING, unit="rows"))
        stage_dataset = stage / "dataset_version"
        stage_canonical = stage / "canonical_profile"
        built = _build_artifacts(
            raw_final,
            dataset_version_id,
            semantic_fields,
            canonical_fields,
            dialect,
            encoding,
            stage_dataset,
            stage_canonical,
            canonical_profile_id,
            progress,
            on_error=policy,
            malformed_handler=malformed_handler,
            escalation_handler=escalation_handler,
        )
        row_count = built.accepted_count
        rejected_count = built.rejected_count
        record_path = built.record_path
        canonical_path = built.canonical_path
        reject_path = built.reject_path
        normalization_stats = built.normalization_stats
        posting_count = 0
        trigger_fault("import.artifacts_staged")
        workspace.catalog.set_operation_status(operation_id, OperationStatus.VALIDATING.value)
        if progress is not None:
            progress(ImportProgress(OperationStatus.VALIDATING))

        record_metadata = pq.read_metadata(record_path)
        canonical_metadata = pq.read_metadata(canonical_path)
        if record_metadata.num_rows != row_count or canonical_metadata.num_rows != row_count:
            raise ImportFailure("Parquet row count does not match parsed row count")
        if record_metadata.num_row_groups != canonical_metadata.num_row_groups:
            raise ImportFailure("record and canonical Parquet row groups are not aligned")
        for row_group in range(record_metadata.num_row_groups):
            if (
                record_metadata.row_group(row_group).num_rows
                != canonical_metadata.row_group(row_group).num_rows
            ):
                raise ImportFailure("record and canonical Parquet row groups are not aligned")
        record_sha256, record_size = hash_file(record_path)
        canonical_sha256, canonical_size = hash_file(canonical_path)
        if reject_path is not None:
            _validate_reject_artifact(reject_path, rejected_count)
            reject_digest_size = hash_file(reject_path)
        else:
            reject_digest_size = None
        trigger_fault("import.artifacts_validated")
        final_dataset_dir = workspace.root / "datasets" / dataset_id / dataset_version_id
        final_canonical_dir = workspace.root / "canonical/v1" / canonical_profile_id
        final_dataset_dir.parent.mkdir(parents=True, exist_ok=True)
        final_canonical_dir.parent.mkdir(parents=True, exist_ok=True)
        fsync_directory(workspace.root / "datasets")
        if final_dataset_dir.exists() or final_canonical_dir.exists():
            raise ImportFailure("immutable artifact target already exists")
        os.replace(stage_dataset, final_dataset_dir)
        fsync_directory(final_dataset_dir.parent)
        trigger_fault("import.record_finalized")
        os.replace(stage_canonical, final_canonical_dir)
        fsync_directory(final_canonical_dir.parent)
        trigger_fault("import.canonical_finalized")

        record_relative = str(
            (final_dataset_dir / "data/part-00000.parquet").relative_to(workspace.root)
        )
        canonical_relative = str(
            (final_canonical_dir / "part-00000.parquet").relative_to(workspace.root)
        )
        reject_relative = (
            str(
                (final_dataset_dir / "rejects/rejected-records.jsonl").relative_to(workspace.root)
            )
            if reject_path is not None
            else None
        )
        artifact_rows = [
            (
                str(uuid.uuid4()),
                dataset_version_id,
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
                dataset_version_id,
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
                    dataset_version_id,
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
        trigger_fault("import.before_catalog_commit")
        with workspace.catalog.transaction() as connection:
            for custom_type in resolved_plan.custom_types:
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
                    id, dataset_version_id, status, fingerprint, spec_json,
                    statistics_json, created_at, published_at
                ) VALUES (?, ?, 'PUBLISHED', ?, ?, ?, ?, ?)
                """,
                (
                    canonical_profile_id,
                    dataset_version_id,
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
                    id, dataset_version_id, canonical_profile_id, status, fingerprint,
                    policy_json, policy_version, posting_count, created_at, published_at
                ) VALUES (?, ?, ?, 'PUBLISHED', ?, ?, ?, ?, ?, ?)
                """,
                (
                    index_profile_id,
                    dataset_version_id,
                    canonical_profile_id,
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
                SET status = ?, row_count = ?, published_at = ?, normalization_stats_json = ?,
                    recipe_json = ?
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
                    dataset_version_id,
                ),
            )
            connection.execute(
                """
                UPDATE datasets SET active_version_id = ?, active_canonical_profile_id = ?,
                                    active_index_profile_id = ? WHERE id = ?
                """,
                (dataset_version_id, canonical_profile_id, index_profile_id, dataset_id),
            )
            connection.execute(
                "UPDATE operations SET status = ?, updated_at = ? WHERE id = ?",
                (OperationStatus.PUBLISHED.value, published_at, operation_id),
            )
        trigger_fault("import.after_catalog_commit")
        # The catalog transaction above is the commit point.
        with contextlib.suppress(OSError):
            stage.rmdir()
        if progress is not None:
            progress(ImportProgress(OperationStatus.PUBLISHED, 1, 1, "version"))
        return ImportResult(
            operation_id=operation_id,
            dataset_id=dataset_id,
            dataset_name=name,
            dataset_version_id=dataset_version_id,
            source_sha256=source_sha256,
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
        try:
            with workspace.catalog.transaction() as connection:
                connection.execute(
                    """
                    UPDATE operations
                    SET status = ?, updated_at = ?, error = ?
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
                        dataset_version_id,
                        OperationStatus.PUBLISHED.value,
                    ),
                )
        except Exception:
            pass
        if isinstance(error, ImportFailure):
            raise
        raise ImportFailure(f"import failed before publication: {error}") from error


def import_csv(
    workspace: Workspace,
    source_path: str | Path,
    *,
    dataset_name: str | None = None,
    encoding: str = "utf-8",
    schema_path: Path | None = None,
    schema_plan: SchemaPlan | None = None,
    progress: Callable[[ImportProgress], None] | None = None,
    on_error: MalformedRecordPolicy | str | None = None,
    malformed_handler: MalformedRecordHandler | None = None,
    escalation_handler: RejectEscalationHandler | None = None,
) -> ImportResult:
    """Import a CSV while holding the workspace's single-writer lock."""
    try:
        source_path = Path(source_path).expanduser().resolve(strict=True)
    except OSError as error:
        raise ImportFailure(f"cannot access source file: {error}") from error
    if schema_path is not None and schema_plan is not None:
        raise ImportFailure("pass either schema_path or schema_plan, not both")
    if on_error is not None:
        try:
            MalformedRecordPolicy(on_error)
        except ValueError:
            raise ImportFailure(f"unknown malformed-record policy: {on_error}") from None
    if schema_plan is None:
        if schema_path is None:
            raise ImportFailure("a fully confirmed schema is required before import")
        schema_plan = load_schema_document(schema_path)
        schema_path = None
    if not schema_plan.confirmed:
        raise ImportFailure("schema plan is not fully confirmed")
    existing_custom = workspace.catalog.list_custom_types()
    active_custom = workspace.catalog.list_custom_types(include_retired=False)
    retired = set(existing_custom) - set(active_custom)
    selected = {str(field.selected_type) for field in schema_plan.fields}
    blocked = sorted(retired & (selected | {item.type_id for item in schema_plan.custom_types}))
    if blocked:
        raise ImportFailure(
            "custom semantic type is retired; restore before import with: "
            + "; ".join(f"pandoracle types restore {item}" for item in blocked)
        )
    for definition in schema_plan.custom_types:
        existing = existing_custom.get(definition.type_id)
        if existing is not None and existing != definition:
            raise ImportFailure(
                f"custom type conflicts with workspace definition: {definition.type_id}"
            )
    with workspace.lock(exclusive=True):
        return _import_csv_unlocked(
            workspace,
            source_path,
            dataset_name=dataset_name,
            encoding=encoding,
            schema_path=schema_path,
            schema_plan=schema_plan,
            progress=progress,
            on_error=on_error,
            malformed_handler=malformed_handler,
            escalation_handler=escalation_handler,
        )
