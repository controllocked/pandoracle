from __future__ import annotations

import contextlib
import hashlib
import json
import os
import statistics as statistics_module
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq

from pandoracle.acceleration import (
    ACCELERATION_FORMAT_VERSION,
    AccelerationPosting,
    AccelerationWriter,
    verify_acceleration,
)
from pandoracle.errors import ImportFailure
from pandoracle.faults import trigger_fault
from pandoracle.fs import fsync_directory, hash_file, utc_now
from pandoracle.models import (
    AccelerationKind,
    CanonicalFieldSpec,
    FieldAccelerationPolicy,
    SearchOperator,
)
from pandoracle.workspace import Workspace

ACCELERATION_POLICY_VERSION = 1
TOKEN_SAMPLE_SIZE = 4096
TOKEN_SAMPLE_ROW_GROUPS = 16
BUILD_BATCH_ROWS = 65_536


@dataclass(frozen=True)
class AccelerationEstimate:
    field_id: int
    primitive: AccelerationKind
    posting_count: int
    key_bytes: int
    size_bytes: int
    seconds: float
    basis: str
    projected_bytes: int = 0
    read_seconds: float = 0.0
    processing_seconds: float = 0.0
    finalization_seconds: float = 0.0
    token_sample_count: int = 0
    utility_tier: str = "none"
    recommended: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["primitive"] = self.primitive.value
        return value


@dataclass(frozen=True)
class AccelerationPlan:
    dataset_id: str
    dataset_version_id: str
    canonical_profile_id: str
    canonical_fingerprint: str
    fields: tuple[FieldAccelerationPolicy, ...]
    estimates: tuple[AccelerationEstimate, ...]
    confirmed: bool = False
    plan_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "acceleration_plan_version": self.plan_version,
            "confirmed": self.confirmed,
            "dataset_id": self.dataset_id,
            "dataset_version_id": self.dataset_version_id,
            "canonical_profile_id": self.canonical_profile_id,
            "canonical_fingerprint": self.canonical_fingerprint,
            "fields": [item.to_dict() for item in self.fields],
            "estimates": [item.to_dict() for item in self.estimates],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AccelerationPlan:
        if int(value.get("acceleration_plan_version", -1)) != 1:
            raise ImportFailure("unsupported acceleration plan version")
        return cls(
            dataset_id=str(value["dataset_id"]),
            dataset_version_id=str(value["dataset_version_id"]),
            canonical_profile_id=str(value["canonical_profile_id"]),
            canonical_fingerprint=str(value["canonical_fingerprint"]),
            fields=tuple(FieldAccelerationPolicy.from_dict(item) for item in value["fields"]),
            estimates=tuple(
                AccelerationEstimate(
                    field_id=int(item["field_id"]),
                    primitive=AccelerationKind(item["primitive"]),
                    posting_count=int(item["posting_count"]),
                    key_bytes=int(item["key_bytes"]),
                    size_bytes=int(item["size_bytes"]),
                    seconds=float(item["seconds"]),
                    basis=str(item["basis"]),
                    projected_bytes=int(item["projected_bytes"]),
                    read_seconds=float(item["read_seconds"]),
                    processing_seconds=float(item["processing_seconds"]),
                    finalization_seconds=float(item["finalization_seconds"]),
                    token_sample_count=int(item["token_sample_count"]),
                    utility_tier=str(item["utility_tier"]),
                    recommended=bool(item["recommended"]),
                )
                for item in value["estimates"]
            ),
            confirmed=bool(value["confirmed"]),
        )


@dataclass(frozen=True)
class AccelerationProgress:
    phase: str
    completed: int
    total: int
    unit: str = "rows"
    primitive: str | None = None
    field_id: int | None = None
    overall_completed: float | None = None
    overall_total: float | None = None
    observed_rate: float | None = None
    estimated_remaining_seconds: float | None = None
    estimate_basis: str = "initial"


@dataclass(frozen=True)
class AccelerationResult:
    operation_id: str
    dataset_id: str
    dataset_name: str
    dataset_version_id: str
    canonical_profile_id: str
    index_profile_id: str
    posting_count: int
    artifact_bytes: int
    deduplicated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_acceleration_plan(path: Path) -> AccelerationPlan:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ImportFailure(f"invalid acceleration plan file: {error}") from error
    if not isinstance(value, dict):
        raise ImportFailure("acceleration plan must contain a JSON object")
    try:
        return AccelerationPlan.from_dict(value)
    except ImportFailure:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise ImportFailure(
            "invalid acceleration plan; create it again with 'pandoracle acceleration plan'"
        ) from error


def confirm_acceleration_plan(
    plan: AccelerationPlan,
    selections: Mapping[int, tuple[str | AccelerationKind, ...]],
) -> AccelerationPlan:
    fields = []
    for field in plan.fields:
        requested = selections.get(field.field_id, field.accelerations)
        fields.append(
            replace(field, accelerations=tuple(requested), selection_source="user_confirmed")
        )
    return replace(plan, fields=tuple(fields), confirmed=True)


def parse_policy(value: str) -> list[FieldAccelerationPolicy]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or decoded.get("policy_version") != 1:
        raise ImportFailure("unsupported acceleration policy")
    return [FieldAccelerationPolicy.from_dict(item) for item in decoded["fields"]]


def policy_json(fields: tuple[FieldAccelerationPolicy, ...]) -> str:
    return json.dumps(
        {"policy_version": 1, "fields": [item.to_dict() for item in fields]},
        sort_keys=True,
    )


def policy_fingerprint(
    canonical_profile_id: str, fields: tuple[FieldAccelerationPolicy, ...]
) -> str:
    payload = {
        "format_version": ACCELERATION_FORMAT_VERSION,
        "canonical_profile_id": canonical_profile_id,
        "fields": [
            {
                "field_id": item.field_id,
                "semantic_type": item.semantic_type,
                "accelerations": [primitive.value for primitive in item.accelerations],
            }
            for item in fields
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _context(workspace: Workspace, identifier: str) -> dict[str, Any]:
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        row = connection.execute(
            """
            SELECT d.id AS dataset_id, d.name AS dataset_name,
                   d.active_version_id, d.active_canonical_profile_id,
                   d.active_index_profile_id, v.row_count,
                   cp.fingerprint AS canonical_fingerprint, cp.spec_json,
                   cp.statistics_json, ip.fingerprint AS index_fingerprint,
                   ip.policy_json, ip.posting_count,
                   ac.relative_path AS canonical_path
            FROM datasets d
            JOIN dataset_versions v ON v.id=d.active_version_id
            JOIN canonical_profiles cp ON cp.id=d.active_canonical_profile_id
            JOIN index_profiles ip ON ip.id=d.active_index_profile_id
            JOIN artifacts ac ON ac.canonical_profile_id=cp.id
              AND ac.kind IN ('CANONICAL_PARQUET','NORMALIZED_PARQUET')
            WHERE d.id=? OR d.name=?
            """,
            (identifier, identifier),
        ).fetchone()
        artifact_bytes = connection.execute(
            "SELECT COALESCE(SUM(size_bytes),0) FROM artifacts WHERE dataset_version_id="
            "(SELECT active_version_id FROM datasets WHERE id=? OR name=?) "
            "AND kind IN ('RECORD_PARQUET','CANONICAL_PARQUET','NORMALIZED_PARQUET')",
            (identifier, identifier),
        ).fetchone()[0]
    if row is None:
        raise ImportFailure(f"dataset not found or has no active canonical profile: {identifier}")
    result = dict(row)
    result["artifact_bytes"] = int(artifact_bytes or 0)
    return result


def _supported(spec: CanonicalFieldSpec) -> tuple[AccelerationKind, ...]:
    result = []
    operators = set(spec.supported_operators)
    if SearchOperator.EXACT.value in operators or SearchOperator.RANGE.value in operators:
        result.append(AccelerationKind.VALUE)
    if SearchOperator.TOKEN.value in operators:
        result.append(AccelerationKind.TOKEN)
    return tuple(result)


def _column_compressed_bytes(parquet: pq.ParquetFile, column: str) -> int:
    index = parquet.schema_arrow.names.index(column)
    return sum(
        parquet.metadata.row_group(group).column(index).total_compressed_size
        for group in range(parquet.num_row_groups)
    )


def _planning_statistics(
    workspace: Workspace,
    context: dict[str, Any],
    specs: list[CanonicalFieldSpec],
) -> tuple[dict[str, dict[str, Any]], dict[int, int]]:
    statistics = {
        str(field_id): dict(value)
        for field_id, value in json.loads(context["statistics_json"] or "{}").items()
    }
    canonical_path = workspace.resolve_relative(str(context["canonical_path"]))
    parquet = pq.ParquetFile(canonical_path)
    projected = {
        spec.field_id: _column_compressed_bytes(parquet, spec.canonical_storage_name)
        for spec in specs
    }
    needs_sample = [
        spec
        for spec in specs
        if "canonical_utf8_bytes" not in statistics.get(str(spec.field_id), {})
        or (
            spec.tokenizer_id is not None
            and (
                "estimated_token_postings" not in statistics.get(str(spec.field_id), {})
                or "estimated_token_key_bytes" not in statistics.get(str(spec.field_id), {})
            )
        )
    ]
    samples: dict[int, list[str]] = {spec.field_id: [] for spec in needs_sample}
    if needs_sample:
        columns = [spec.canonical_storage_name for spec in needs_sample]
        group_count = min(TOKEN_SAMPLE_ROW_GROUPS, parquet.num_row_groups)
        if group_count == 0:
            row_groups = []
        elif group_count == 1:
            row_groups = [0]
        else:
            row_groups = sorted(
                {
                    round(index * (parquet.num_row_groups - 1) / (group_count - 1))
                    for index in range(group_count)
                }
            )
        values_per_group = max(1, (TOKEN_SAMPLE_SIZE + len(row_groups) - 1) // len(row_groups))
        for row_group in row_groups:
            batches = parquet.iter_batches(
                columns=columns,
                row_groups=[row_group],
                batch_size=values_per_group,
            )
            batch = next(batches, None)
            if batch is None:
                continue
            for spec in needs_sample:
                values = samples[spec.field_id]
                if len(values) >= TOKEN_SAMPLE_SIZE:
                    continue
                for value in batch[spec.canonical_storage_name].to_pylist():
                    if value is not None:
                        values.append(str(value))
                        if len(values) == TOKEN_SAMPLE_SIZE:
                            break
    for spec in specs:
        stats = statistics.setdefault(str(spec.field_id), {})
        normalized = int(stats.get("normalized", 0))
        values = samples.get(spec.field_id, [])
        sources = []
        if "canonical_utf8_bytes" not in stats:
            average = (
                sum(len(value.encode("utf-8")) for value in values) / len(values)
                if values
                else 32.0
            )
            stats["canonical_utf8_bytes"] = round(normalized * average)
            sources.append(f"{len(values)}-value canonical sample" if values else "fallback")
        else:
            sources.append("exact canonical byte count")
        if spec.tokenizer_id is not None:
            if (
                "estimated_token_postings" not in stats
                or "estimated_token_key_bytes" not in stats
            ):
                token_lists = [set(value.split()) for value in values]
                scale = normalized / len(values) if values else 0.0
                stats["estimated_token_postings"] = round(
                    sum(len(tokens) for tokens in token_lists) * scale
                )
                stats["estimated_token_key_bytes"] = round(
                    sum(
                        len(token.encode("utf-8"))
                        for tokens in token_lists
                        for token in tokens
                    )
                    * scale
                )
                stats["token_sample_count"] = len(values)
                sources.append(f"{len(values)}-value TOKEN sample")
            else:
                sources.append(
                    f"{int(stats.get('token_sample_count', 0))}-value TOKEN sample"
                )
        stats["_estimate_basis"] = " + ".join(sources)
    return statistics, projected


def _build_time_model(workspace: Workspace) -> dict[str, Any]:
    processing_rates: dict[str, list[float]] = {item.value: [] for item in AccelerationKind}
    finalization_rates: dict[str, list[float]] = {item.value: [] for item in AccelerationKind}
    read_rates: list[float] = []
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        rows = connection.execute(
            "SELECT ip.statistics_json,ip.build_metrics_json "
            "FROM index_profiles ip WHERE ip.status IN ('PUBLISHED','SUPERSEDED','RETIRED') "
            "AND EXISTS (SELECT 1 FROM artifacts a WHERE a.index_profile_id=ip.id "
            "AND a.kind IN ('VALUE_ACCELERATION','TOKEN_ACCELERATION') "
            "AND a.format_version=1) ORDER BY ip.published_at DESC LIMIT 25"
        ).fetchall()
    for row in rows:
        statistics = json.loads(row["statistics_json"] or "{}")
        metrics = json.loads(row["build_metrics_json"] or "{}")
        payload = sum(
            int(field.get("key_bytes", 0)) + int(field.get("posting_count", 0)) * 8
            for primitive in statistics.values()
            for field in primitive.values()
        )
        wall = float(metrics.get("wall_seconds", 0))
        row_evaluations = sum(
            int(field.get("rows", 0)) for field in metrics.get("field_metrics", {}).values()
        ) or int(metrics.get("posting_count", 0))
        qualifies = wall >= 1.0 and (
            payload >= 64 * 1024 * 1024 or row_evaluations >= 1_000_000
        )
        if not qualifies:
            continue
        for key, field in metrics.get("field_metrics", {}).items():
            primitive = key.split(":", 1)[0]
            seconds = sum(
                float(field.get(name, 0))
                for name in (
                    "tokenization_seconds",
                    "posting_generation_seconds",
                    "sqlite_write_seconds",
                )
            )
            field_payload = int(field.get("key_bytes", 0)) + int(
                field.get("posting_count", 0)
            ) * 8
            if primitive in processing_rates and seconds > 0 and field_payload > 0:
                processing_rates[primitive].append(field_payload / seconds)
        for primitive, value in metrics.get("primitive_metrics", {}).items():
            seconds = float(value.get("finalization_seconds", 0))
            artifact_bytes = int(value.get("artifact_bytes", 0))
            if primitive in finalization_rates and seconds > 0 and artifact_bytes > 0:
                finalization_rates[primitive].append(artifact_bytes / seconds)
        phases = metrics.get("phases", {})
        read_seconds = float(phases.get("canonical_read_seconds", 0))
        read_bytes = int(metrics.get("canonical_projected_bytes", 0))
        if read_seconds > 0 and read_bytes > 0:
            read_rates.append(read_bytes / read_seconds)
    return {
        "processing_rates": {
            primitive: statistics_module.median(values[:5])
            for primitive, values in processing_rates.items()
            if values
        },
        "processing_samples": {
            primitive: min(5, len(values)) for primitive, values in processing_rates.items()
        },
        "finalization_rates": {
            primitive: statistics_module.median(values[:5])
            for primitive, values in finalization_rates.items()
            if values
        },
        "read_rate": statistics_module.median(read_rates[:5]) if read_rates else None,
    }


def _estimate(
    workspace: Workspace,
    field: CanonicalFieldSpec,
    primitive: AccelerationKind,
    stats: dict[str, Any],
    projected_bytes: int,
    time_model: dict[str, Any],
) -> AccelerationEstimate:
    normalized = int(stats.get("normalized", 0))
    canonical_bytes = int(stats.get("canonical_utf8_bytes", normalized * 32))
    if primitive is AccelerationKind.VALUE:
        postings, key_bytes = normalized, canonical_bytes
    else:
        postings = int(stats.get("estimated_token_postings", normalized * 2))
        key_bytes = int(stats.get("estimated_token_key_bytes", postings * 12))
    payload = key_bytes + postings * 8
    size_history: list[float] = []
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        rows = connection.execute(
            "SELECT ip.statistics_json,ip.build_metrics_json,a.size_bytes "
            "FROM index_profiles ip JOIN artifacts a ON a.index_profile_id=ip.id "
            "WHERE ip.status IN ('PUBLISHED','SUPERSEDED','RETIRED') "
            "AND a.kind=? AND a.format_version=1 ORDER BY ip.published_at DESC LIMIT 5",
            (f"{primitive.value}_ACCELERATION",),
        ).fetchall()
    for row in rows:
        build_statistics = json.loads(row["statistics_json"] or "{}")
        primitive_stats = build_statistics.get(primitive.value, {})
        observed_payload = sum(
            int(item.get("key_bytes", 0)) + int(item.get("posting_count", 0)) * 8
            for item in primitive_stats.values()
        )
        if observed_payload > 0:
            size_history.append(int(row["size_bytes"]) / observed_payload)
    size_ratio = statistics_module.median(size_history) if size_history else 1.35
    size = 4096 if postings == 0 else max(16_384, int(payload * size_ratio))
    processing_rate = time_model["processing_rates"].get(primitive.value)
    finalization_rate = time_model["finalization_rates"].get(primitive.value)
    read_rate = time_model["read_rate"]
    if processing_rate:
        processing_seconds = payload / processing_rate
        read_seconds = projected_bytes / (read_rate or 150_000_000.0)
        finalization_seconds = size / (finalization_rate or 100_000_000.0)
        history_basis = (
            f"{time_model['processing_samples'][primitive.value]} detailed local build(s)"
        )
    else:
        baseline = 1_500_000.0 if primitive is AccelerationKind.VALUE else 1_000_000.0
        processing_seconds = payload / baseline
        read_seconds = projected_bytes / 150_000_000.0
        finalization_seconds = size / 100_000_000.0
        history_basis = "conservative primitive baseline"
    seconds = (
        0.0
        if postings == 0
        else max(0.1, read_seconds + processing_seconds + finalization_seconds)
    )
    basis = f"{stats.get('_estimate_basis', 'dataset statistics')} + {history_basis}"
    default = field.default_operator
    high = (primitive is AccelerationKind.TOKEN and field.semantic_type == "PERSON_NAME") or (
        primitive is AccelerationKind.VALUE
        and field.semantic_type in {"EMAIL", "PHONE", "USERNAME", "IP", "DOMAIN", "URL"}
    )
    medium = primitive is AccelerationKind.VALUE and field.semantic_type in {
        "PERSON_NAME",
        "DATE",
        "DATE_OF_BIRTH",
    }
    custom = field.semantic_type not in {
        "UNKNOWN",
        "PERSON_NAME",
        "DATE",
        "DATE_OF_BIRTH",
        "EMAIL",
        "PHONE",
        "USERNAME",
        "IP",
        "DOMAIN",
        "URL",
    } and (
        (primitive is AccelerationKind.TOKEN and default == SearchOperator.TOKEN.value)
        or (primitive is AccelerationKind.VALUE and default == SearchOperator.EXACT.value)
    )
    utility_tier = "high" if high else "medium" if medium else "custom" if custom else "none"
    return AccelerationEstimate(
        field.field_id,
        primitive,
        postings,
        key_bytes,
        size,
        seconds,
        basis,
        projected_bytes=projected_bytes,
        read_seconds=read_seconds,
        processing_seconds=processing_seconds,
        finalization_seconds=finalization_seconds,
        token_sample_count=int(stats.get("token_sample_count", 0)),
        utility_tier=utility_tier,
        recommended=(high or medium or custom) and normalized > 0,
    )


def make_acceleration_plan(workspace: Workspace, identifier: str) -> AccelerationPlan:
    context = _context(workspace, identifier)
    specs = [CanonicalFieldSpec.from_dict(item) for item in json.loads(context["spec_json"])]
    statistics, projected = _planning_statistics(workspace, context, specs)
    time_model = _build_time_model(workspace)
    estimates = tuple(
        _estimate(
            workspace,
            spec,
            primitive,
            statistics.get(str(spec.field_id), {}),
            projected[spec.field_id],
            time_model,
        )
        for spec in specs
        for primitive in _supported(spec)
    )
    storage_budget = max(
        256 * 1024 * 1024,
        min(8 * 1024**3, int(context["artifact_bytes"] * 0.25)),
    )
    selected: dict[int, list[AccelerationKind]] = {spec.field_id: [] for spec in specs}
    used_bytes = 0
    used_seconds = 0.0
    ordered = sorted(
        (item for item in estimates if item.recommended),
        key=lambda item: (
            {"high": 0, "medium": 1, "custom": 2}.get(item.utility_tier, 3),
            item.size_bytes,
            item.field_id,
            item.primitive.value,
        ),
    )
    for item in ordered:
        if used_bytes + item.size_bytes > storage_budget or used_seconds + item.seconds > 600:
            continue
        selected[item.field_id].append(item.primitive)
        used_bytes += item.size_bytes
        used_seconds += item.seconds
    fields = tuple(
        FieldAccelerationPolicy(
            spec.field_id,
            spec.semantic_type,
            tuple(selected[spec.field_id]),
            proposal_source="costed_recommendation",
            selection_source="proposal",
        )
        for spec in specs
    )
    return AccelerationPlan(
        str(context["dataset_id"]),
        str(context["active_version_id"]),
        str(context["active_canonical_profile_id"]),
        str(context["canonical_fingerprint"]),
        fields,
        estimates,
    )


def configure_acceleration(
    workspace: Workspace,
    identifier: str,
    plan: AccelerationPlan,
    *,
    force: bool = False,
    progress: Callable[[AccelerationProgress], None] | None = None,
) -> AccelerationResult:
    if not plan.confirmed:
        raise ImportFailure("acceleration plan is not fully confirmed")
    with workspace.lock(exclusive=True):
        context = _context(workspace, identifier)
        if (
            plan.dataset_id != context["dataset_id"]
            or plan.dataset_version_id != context["active_version_id"]
            or plan.canonical_profile_id != context["active_canonical_profile_id"]
            or plan.canonical_fingerprint != context["canonical_fingerprint"]
        ):
            raise ImportFailure("acceleration plan is stale; create it from the active dataset")
        specs = [CanonicalFieldSpec.from_dict(item) for item in json.loads(context["spec_json"])]
        specs_by_id = {item.field_id: item for item in specs}
        if {field.field_id for field in plan.fields} != set(specs_by_id) or len(plan.fields) != len(
            specs_by_id
        ):
            raise ImportFailure("acceleration plan fields do not match the canonical profile")
        for field in plan.fields:
            supported = set(_supported(specs_by_id[field.field_id]))
            unsupported = set(field.accelerations) - supported
            if unsupported:
                raise ImportFailure(
                    f"field {field.field_id} does not support "
                    + ", ".join(sorted(item.value for item in unsupported))
                )
        fingerprint = policy_fingerprint(plan.canonical_profile_id, plan.fields)
        operation_id = str(uuid.uuid4())
        if not force and fingerprint == context["index_fingerprint"]:
            with workspace.catalog.transaction() as connection:
                now = utc_now()
                connection.execute(
                    "INSERT INTO operations(id,kind,status,dataset_id,dataset_version_id,"
                    "started_at,updated_at) VALUES "
                    "(?, 'ACCELERATION_REVISION','PUBLISHED',?,?,?,?)",
                    (operation_id, plan.dataset_id, plan.dataset_version_id, now, now),
                )
            return AccelerationResult(
                operation_id,
                plan.dataset_id,
                str(context["dataset_name"]),
                plan.dataset_version_id,
                plan.canonical_profile_id,
                str(context["active_index_profile_id"]),
                int(context["posting_count"]),
                0,
                True,
            )

        profile_id = str(uuid.uuid4())
        stage = workspace.root / "operations/staging" / operation_id
        stage_acceleration = stage / "acceleration"
        stage_acceleration.mkdir(parents=True, mode=0o700)
        now = utc_now()
        with workspace.catalog.transaction() as connection:
            connection.execute(
                "INSERT INTO operations(id,kind,status,dataset_id,dataset_version_id,"
                "started_at,updated_at) VALUES (?, 'ACCELERATION_REVISION','INDEXING',?,?,?,?)",
                (operation_id, plan.dataset_id, plan.dataset_version_id, now, now),
            )
            connection.execute(
                """INSERT INTO index_profiles(
                    id,dataset_version_id,canonical_profile_id,parent_profile_id,status,
                    fingerprint,policy_json,policy_version,created_at
                ) VALUES (?,?,?,?, 'INDEXING',?,?,?,?)""",
                (
                    profile_id,
                    plan.dataset_version_id,
                    plan.canonical_profile_id,
                    context["active_index_profile_id"],
                    fingerprint,
                    policy_json(plan.fields),
                    ACCELERATION_POLICY_VERSION,
                    now,
                ),
            )
        trigger_fault("acceleration.profile_cataloged")
        writers: dict[AccelerationKind, AccelerationWriter] = {}
        started = time.perf_counter()
        try:
            for primitive in AccelerationKind:
                if any(primitive in field.accelerations for field in plan.fields):
                    writers[primitive] = AccelerationWriter(
                        stage_acceleration / f"{primitive.value.lower()}.sqlite",
                        primitive=primitive,
                        dataset_version_id=plan.dataset_version_id,
                        canonical_profile_id=plan.canonical_profile_id,
                        index_profile_id=profile_id,
                        policy_fingerprint=fingerprint,
                    )
            selected_specs = {
                primitive: [
                    specs_by_id[field.field_id]
                    for field in plan.fields
                    if primitive in field.accelerations
                ]
                for primitive in writers
            }
            selected_estimates = {
                (item.field_id, item.primitive): item
                for item in plan.estimates
                if item.primitive in writers
                and any(
                    field.field_id == item.field_id
                    and item.primitive in field.accelerations
                    for field in plan.fields
                )
            }
            unique_read_seconds = sum(
                max(
                    (
                        estimate.read_seconds
                        for (field_id, _), estimate in selected_estimates.items()
                        if field_id == selected_field_id
                    ),
                    default=0.0,
                )
                for selected_field_id in {
                    field_id for field_id, _ in selected_estimates
                }
            )
            initial_estimate_seconds = max(
                0.1,
                unique_read_seconds
                + sum(
                    estimate.processing_seconds + estimate.finalization_seconds
                    for estimate in selected_estimates.values()
                ),
            )
            initial_finalization_seconds = sum(
                estimate.finalization_seconds for estimate in selected_estimates.values()
            )
            phases = {
                "canonical_read_seconds": 0.0,
                # Canonical Parquet already contains normalized values. This is
                # deliberately explicit so the persisted breakdown cannot imply
                # that acceleration rebuilds recanonicalize source records.
                "normalization_seconds": 0.0,
                "tokenization_seconds": 0.0,
                "posting_generation_seconds": 0.0,
                "sqlite_write_seconds": 0.0,
                "index_btree_finalization_seconds": 0.0,
                "validation_seconds": 0.0,
                "checksum_seconds": 0.0,
                "filesystem_publication_seconds": 0.0,
                "catalog_publication_seconds": 0.0,
            }
            field_metrics: dict[str, dict[str, Any]] = {
                f"{primitive.value}:{spec.field_id}": {
                    "primitive": primitive.value,
                    "field_id": spec.field_id,
                    "rows": 0,
                    "tokenization_seconds": 0.0,
                    "posting_generation_seconds": 0.0,
                    "sqlite_write_seconds": 0.0,
                }
                for primitive, selected in selected_specs.items()
                for spec in selected
            }
            predicted_finish = started + initial_estimate_seconds
            current_estimate_basis = "initial"

            def report(
                phase: str,
                completed_rows: int,
                *,
                primitive: AccelerationKind | None = None,
                field_id: int | None = None,
                observed_rate: float | None = None,
                remaining_seconds: float | None = None,
                basis: str | None = None,
            ) -> None:
                nonlocal current_estimate_basis, predicted_finish
                if progress is None:
                    return
                now = time.perf_counter()
                elapsed_seconds = now - started
                if remaining_seconds is not None:
                    predicted_finish = now + max(0.0, remaining_seconds)
                if basis is not None:
                    current_estimate_basis = basis
                remaining = max(0.0, predicted_finish - now)
                progress(
                    AccelerationProgress(
                        phase,
                        completed_rows,
                        int(context["row_count"]),
                        primitive=primitive.value if primitive else None,
                        field_id=field_id,
                        overall_completed=elapsed_seconds,
                        overall_total=max(elapsed_seconds, elapsed_seconds + remaining),
                        observed_rate=observed_rate,
                        estimated_remaining_seconds=remaining,
                        estimate_basis=current_estimate_basis,
                    )
                )

            completed = 0
            batch_count = 0
            batches: Any = iter(())
            parquet: pq.ParquetFile | None = None
            canonical_projected_bytes = 0
            if writers:
                canonical_path = workspace.resolve_relative(str(context["canonical_path"]))
                parquet = pq.ParquetFile(canonical_path)
                selected_columns = [
                    "record_ordinal",
                    *sorted(
                        {
                            spec.canonical_storage_name
                            for values in selected_specs.values()
                            for spec in values
                        }
                    ),
                ]
                canonical_projected_bytes = sum(
                    _column_compressed_bytes(parquet, column) for column in selected_columns
                )
                batches = parquet.iter_batches(
                    columns=selected_columns,
                    batch_size=BUILD_BATCH_ROWS,
                )
            while True:
                read_started = time.perf_counter()
                try:
                    batch = next(batches)
                except StopIteration:
                    break
                phases["canonical_read_seconds"] += time.perf_counter() - read_started
                ordinal_started = time.perf_counter()
                ordinals = batch["record_ordinal"].to_pylist()
                ordinal_seconds = time.perf_counter() - ordinal_started
                phases["posting_generation_seconds"] += ordinal_seconds
                if AccelerationKind.VALUE in writers:
                    for spec in selected_specs[AccelerationKind.VALUE]:
                        key = f"{AccelerationKind.VALUE.value}:{spec.field_id}"
                        generation_started = time.perf_counter()
                        values = batch[spec.canonical_storage_name].to_pylist()
                        postings = [
                            AccelerationPosting(spec.field_id, str(value), int(ordinal))
                            for ordinal, value in zip(ordinals, values, strict=True)
                            if value is not None
                        ]
                        generation_seconds = time.perf_counter() - generation_started
                        phases["posting_generation_seconds"] += generation_seconds
                        field_metrics[key]["posting_generation_seconds"] += generation_seconds
                        write_started = time.perf_counter()
                        writers[AccelerationKind.VALUE].add(postings)
                        write_seconds = time.perf_counter() - write_started
                        phases["sqlite_write_seconds"] += write_seconds
                        field_metrics[key]["sqlite_write_seconds"] += write_seconds
                        field_metrics[key]["rows"] += batch.num_rows
                if AccelerationKind.TOKEN in writers:
                    for spec in selected_specs[AccelerationKind.TOKEN]:
                        key = f"{AccelerationKind.TOKEN.value}:{spec.field_id}"
                        tokenization_started = time.perf_counter()
                        token_lists = pc.utf8_split_whitespace(batch[spec.canonical_storage_name])
                        tokens = pc.list_flatten(token_lists)
                        parents = pc.list_parent_indices(token_lists)
                        token_ordinals = pc.take(batch["record_ordinal"], parents)
                        tokenization_seconds = time.perf_counter() - tokenization_started
                        phases["tokenization_seconds"] += tokenization_seconds
                        field_metrics[key]["tokenization_seconds"] += tokenization_seconds
                        generation_started = time.perf_counter()
                        postings = [
                            AccelerationPosting(spec.field_id, str(token), int(ordinal))
                            for ordinal, token in zip(
                                token_ordinals.to_pylist(), tokens.to_pylist(), strict=True
                            )
                            if token is not None
                        ]
                        generation_seconds = time.perf_counter() - generation_started
                        phases["posting_generation_seconds"] += generation_seconds
                        field_metrics[key]["posting_generation_seconds"] += generation_seconds
                        write_started = time.perf_counter()
                        writers[AccelerationKind.TOKEN].add(postings)
                        write_seconds = time.perf_counter() - write_started
                        phases["sqlite_write_seconds"] += write_seconds
                        field_metrics[key]["sqlite_write_seconds"] += write_seconds
                        field_metrics[key]["rows"] += batch.num_rows
                completed += batch.num_rows
                batch_count += 1
                elapsed_rows = time.perf_counter() - started
                observed_rate = completed / elapsed_rows if elapsed_rows > 0 else None
                if batch_count >= 2 and observed_rate:
                    remaining_rows = int(context["row_count"]) - completed
                    remaining_seconds = (
                        remaining_rows / observed_rate + initial_finalization_seconds
                    )
                    basis = "observed"
                else:
                    remaining_seconds = None
                    basis = "initial"
                current_primitive = next(reversed(selected_specs))
                current_spec = selected_specs[current_primitive][-1]
                report(
                    "BUILDING",
                    completed,
                    primitive=current_primitive,
                    field_id=current_spec.field_id,
                    observed_rate=observed_rate if batch_count >= 2 else None,
                    remaining_seconds=remaining_seconds,
                    basis=basis,
                )
            statistics: dict[str, dict[int, dict[str, int]]] = {}
            primitive_metrics: dict[str, dict[str, Any]] = {}
            for primitive, writer in writers.items():
                report("FINALIZING", completed, primitive=primitive)
                primitive_statistics, finish_metrics = writer.finish()
                for spec in selected_specs[primitive]:
                    primitive_statistics.setdefault(
                        spec.field_id,
                        {"posting_count": 0, "key_bytes": 0},
                    )
                statistics[primitive.value] = primitive_statistics
                primitive_metrics[primitive.value] = {
                    **finish_metrics,
                    "btree_maintenance": "inline_in_sqlite_write",
                    "finalization_seconds": finish_metrics["total_seconds"],
                    "index_btree_finalization_seconds": (
                        finish_metrics["statistics_seconds"]
                        + finish_metrics["commit_seconds"]
                    ),
                }
                phases["index_btree_finalization_seconds"] += (
                    finish_metrics["statistics_seconds"] + finish_metrics["commit_seconds"]
                )
                phases["validation_seconds"] += finish_metrics["integrity_check_seconds"]
                phases["filesystem_publication_seconds"] += finish_metrics["fsync_seconds"]
            for primitive in writers:
                report("VALIDATING", completed, primitive=primitive)
                validation_started = time.perf_counter()
                verify_acceleration(
                    stage_acceleration / f"{primitive.value.lower()}.sqlite",
                    primitive=primitive,
                    dataset_version_id=plan.dataset_version_id,
                    canonical_profile_id=plan.canonical_profile_id,
                    index_profile_id=profile_id,
                    policy_fingerprint=fingerprint,
                    check_integrity=False,
                )
                metadata_validation_seconds = time.perf_counter() - validation_started
                phases["validation_seconds"] += metadata_validation_seconds
                primitive_metrics[primitive.value]["metadata_validation_seconds"] = (
                    metadata_validation_seconds
                )
                primitive_metrics[primitive.value]["finalization_seconds"] += (
                    metadata_validation_seconds
                )
            trigger_fault("acceleration.artifacts_validated")
            final_dir = workspace.root / "accelerations/v1" / profile_id
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            if final_dir.exists():
                raise ImportFailure("immutable acceleration profile target already exists")
            report("PUBLISHING", completed)
            filesystem_started = time.perf_counter()
            os.replace(stage_acceleration, final_dir)
            fsync_directory(final_dir.parent)
            phases["filesystem_publication_seconds"] += (
                time.perf_counter() - filesystem_started
            )
            trigger_fault("acceleration.artifacts_finalized")
            artifacts = []
            total_bytes = 0
            total_postings = 0
            for primitive, field_stats in statistics.items():
                path = final_dir / f"{primitive.lower()}.sqlite"
                checksum_started = time.perf_counter()
                digest, size = hash_file(path)
                checksum_seconds = time.perf_counter() - checksum_started
                phases["checksum_seconds"] += checksum_seconds
                total_bytes += size
                primitive_postings = sum(item["posting_count"] for item in field_stats.values())
                total_postings += primitive_postings
                artifacts.append((primitive, path, digest, size, primitive_postings))
                primitive_metrics[primitive]["checksum_seconds"] = checksum_seconds
                primitive_metrics[primitive]["finalization_seconds"] += checksum_seconds
                primitive_metrics[primitive]["artifact_bytes"] = size
                primitive_metrics[primitive]["posting_count"] = primitive_postings
                for field_id, actual in field_stats.items():
                    field_metrics[f"{primitive}:{field_id}"].update(actual)
            published = utc_now()
            metrics = {
                "metrics_version": 1,
                "wall_seconds": max(time.perf_counter() - started, 1e-9),
                "elapsed_seconds": max(time.perf_counter() - started, 1e-9),
                "artifact_bytes": total_bytes,
                "posting_count": total_postings,
                "canonical_projected_bytes": canonical_projected_bytes,
                "phases": phases,
                "field_metrics": field_metrics,
                "primitive_metrics": primitive_metrics,
            }
            trigger_fault("acceleration.before_catalog_commit")
            catalog_started = time.perf_counter()
            with workspace.catalog.transaction() as connection:
                for primitive, path, digest, size, _ in artifacts:
                    connection.execute(
                        """INSERT INTO artifacts(
                            id,dataset_version_id,kind,format_version,relative_path,sha256,
                            size_bytes,created_at,index_profile_id
                        ) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            str(uuid.uuid4()),
                            plan.dataset_version_id,
                            f"{primitive}_ACCELERATION",
                            ACCELERATION_FORMAT_VERSION,
                            str(path.relative_to(workspace.root)),
                            digest,
                            size,
                            published,
                            profile_id,
                        ),
                    )
                connection.execute(
                    "UPDATE index_profiles SET status='PUBLISHED',posting_count=?,"
                    "statistics_json=?,build_metrics_json=?,published_at=? WHERE id=?",
                    (
                        total_postings,
                        json.dumps(statistics, sort_keys=True),
                        json.dumps(metrics, sort_keys=True),
                        published,
                        profile_id,
                    ),
                )
                connection.execute(
                    "UPDATE index_profiles SET status='SUPERSEDED' WHERE id=? AND id<>?",
                    (context["active_index_profile_id"], profile_id),
                )
                connection.execute(
                    "UPDATE datasets SET active_index_profile_id=? WHERE id=?",
                    (profile_id, plan.dataset_id),
                )
                connection.execute(
                    "UPDATE operations SET status='PUBLISHED',updated_at=? WHERE id=?",
                    (published, operation_id),
                )
            phases["catalog_publication_seconds"] = time.perf_counter() - catalog_started
            metrics["wall_seconds"] = max(time.perf_counter() - started, 1e-9)
            metrics["elapsed_seconds"] = metrics["wall_seconds"]
            with workspace.catalog.transaction() as connection:
                connection.execute(
                    "UPDATE index_profiles SET build_metrics_json=? WHERE id=?",
                    (json.dumps(metrics, sort_keys=True), profile_id),
                )
            trigger_fault("acceleration.after_catalog_commit")
            for primitive, primitive_statistics in statistics.items():
                payload = sum(
                    item["key_bytes"] + item["posting_count"] * 8
                    for item in primitive_statistics.values()
                )
                primitive_metric = primitive_metrics[primitive]
                primitive_seconds = sum(
                    float(field_metrics[f"{primitive}:{field_id}"].get(name, 0.0))
                    for field_id in primitive_statistics
                    for name in (
                        "tokenization_seconds",
                        "posting_generation_seconds",
                        "sqlite_write_seconds",
                    )
                ) + float(primitive_metric.get("total_seconds", 0.0))
                workspace.catalog.record_performance_sample(
                    f"BUILD_{primitive}",
                    1,
                    int(context["row_count"]),
                    payload,
                    primitive_seconds,
                )
            report("PUBLISHED", completed, remaining_seconds=0.0, basis="actual")
            with contextlib.suppress(OSError):
                stage.rmdir()
            return AccelerationResult(
                operation_id,
                plan.dataset_id,
                str(context["dataset_name"]),
                plan.dataset_version_id,
                plan.canonical_profile_id,
                profile_id,
                total_postings,
                total_bytes,
            )
        except BaseException as error:
            for writer in writers.values():
                with contextlib.suppress(Exception):
                    writer.abort()
            with contextlib.suppress(Exception), workspace.catalog.transaction() as connection:
                connection.execute(
                    "UPDATE operations SET status='FAILED',updated_at=?,error=? WHERE id=?",
                    (utc_now(), str(error)[:1000], operation_id),
                )
                connection.execute(
                    "UPDATE index_profiles SET status='FAILED' WHERE id=?", (profile_id,)
                )
            if stage.exists():
                orphan = workspace.root / "operations/orphans" / operation_id
                with contextlib.suppress(OSError):
                    os.replace(stage, orphan)
                    fsync_directory(orphan.parent)
            if isinstance(error, (ImportFailure, KeyboardInterrupt)):
                raise
            raise ImportFailure(f"acceleration build failed before publication: {error}") from error


def disable_acceleration(workspace: Workspace, identifier: str) -> AccelerationResult:
    plan = make_acceleration_plan(workspace, identifier)
    empty = tuple(replace(field, accelerations=()) for field in plan.fields)
    return configure_acceleration(
        workspace, identifier, replace(plan, fields=empty, confirmed=True), force=False
    )


def rebuild_acceleration(
    workspace: Workspace,
    identifier: str,
    *,
    progress: Callable[[AccelerationProgress], None] | None = None,
) -> AccelerationResult:
    context = _context(workspace, identifier)
    current = tuple(parse_policy(context["policy_json"]))
    plan = make_acceleration_plan(workspace, identifier)
    return configure_acceleration(
        workspace,
        identifier,
        replace(plan, fields=current, confirmed=True),
        force=True,
        progress=progress,
    )


def show_acceleration(
    workspace: Workspace, identifier: str, *, history: bool = False
) -> list[dict[str, Any]]:
    context = _context(workspace, identifier)
    where = "ip.dataset_version_id=?" if history else "ip.id=?"
    parameter = context["active_version_id"] if history else context["active_index_profile_id"]
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        rows = connection.execute(
            f"""SELECT ip.*, COALESCE(SUM(a.size_bytes),0) AS artifact_bytes
                FROM index_profiles ip LEFT JOIN artifacts a ON a.index_profile_id=ip.id
                WHERE {where} GROUP BY ip.id ORDER BY ip.created_at DESC""",
            (parameter,),
        ).fetchall()
    return [
        {
            **dict(row),
            "policy": [item.to_dict() for item in parse_policy(str(row["policy_json"]))],
            "statistics": json.loads(row["statistics_json"] or "{}"),
            "build_metrics": json.loads(row["build_metrics_json"] or "{}"),
        }
        for row in rows
    ]
