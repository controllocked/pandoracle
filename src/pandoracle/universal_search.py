from __future__ import annotations

import contextlib
import json
import re
import time
from bisect import bisect_right
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from pandoracle.acceleration import (
    MAX_ROWSET_ORDINALS,
    count_token_ordinals,
    count_value_ordinals,
    intersect_rowsets,
    query_token_ordinals,
    query_value_ordinals,
)
from pandoracle.acceleration_profiles import parse_policy
from pandoracle.contracts import contract_for
from pandoracle.errors import SearchFailure
from pandoracle.models import (
    AccelerationKind,
    CandidateMatch,
    CanonicalFieldSpec,
    Provenance,
    RecordRef,
    SearchCandidate,
    SearchClue,
    SearchOperator,
    SearchRequest,
    SearchResult,
    SemanticFieldSpec,
)
from pandoracle.normalize import normalize_with
from pandoracle.workspace import Workspace

_YEAR = re.compile(r"^(\d{4})(?:\.\.(\d{4}))?$")
_SCAN_PERMISSION_SECONDS = 10.0
_BOOTSTRAP_ROWS = 5_000_000
_BOOTSTRAP_BYTES = 256 * 1024 * 1024
_CANDIDATE_FILTER_ROWS = 250_000


class QueryFieldContract(Protocol):
    @property
    def canonicalizer_id(self) -> str | None: ...

    @property
    def canonicalizer_version(self) -> int | None: ...

    @property
    def supported_operators(self) -> tuple[SearchOperator | str, ...]: ...

    @property
    def default_operator(self) -> SearchOperator | str | None: ...


_QUERY_VALUE_EXPECTATIONS = {
    "EMAIL": "an email address such as person@example.test",
    "PHONE": "a telephone number containing 7 to 15 digits",
    "DOMAIN": "a DNS domain such as example.test",
    "URL": "an HTTP(S) URL without embedded credentials",
    "USERNAME": "a non-empty username without spaces",
    "IP": "an IPv4 or IPv6 address",
    "DATE": (
        "a complete date (YYYY-MM-DD, DD.MM.YYYY, or DD/MM/YYYY); "
        "use AUTO or RANGE for YYYY or YYYY..YYYY"
    ),
    "DATE_OF_BIRTH": (
        "a complete date (YYYY-MM-DD, DD.MM.YYYY, or DD/MM/YYYY); "
        "use AUTO or RANGE for YYYY or YYYY..YYYY"
    ),
    "PERSON_NAME": "a person name containing letters",
}


@dataclass(frozen=True)
class SearchProgress:
    phase: str
    completed: int
    total: int
    unit: str = "rows"


def _semantic_fields(value: str) -> list[SemanticFieldSpec]:
    return [SemanticFieldSpec.from_dict(item) for item in json.loads(value)]


def _segments(workspace: Workspace, dataset: str | None) -> list[dict[str, Any]]:
    resolution = """
        SELECT d.id AS dataset_id,d.name AS dataset_name,
               d.active_version_id,d.active_canonical_profile_id,d.active_index_profile_id,
               v.id AS version_id,v.status AS version_status,
               cp.id AS canonical_profile_id,cp.dataset_version_id AS canonical_version_id,
               cp.status AS canonical_status,
               ip.id AS index_profile_id,ip.dataset_version_id AS index_version_id,
               ip.canonical_profile_id AS index_canonical_id,ip.status AS index_status,
               (SELECT COUNT(*) FROM artifacts a
                WHERE a.dataset_version_id=v.id
                AND a.kind IN ('RECORD_PARQUET','NORMALIZED_PARQUET')) AS record_artifacts,
               (SELECT COUNT(*) FROM artifacts a
                WHERE a.canonical_profile_id=cp.id
                AND a.kind IN ('CANONICAL_PARQUET','NORMALIZED_PARQUET')) AS canonical_artifacts
        FROM datasets d
        LEFT JOIN dataset_versions v ON v.id=d.active_version_id
        LEFT JOIN canonical_profiles cp ON cp.id=d.active_canonical_profile_id
        LEFT JOIN index_profiles ip ON ip.id=d.active_index_profile_id
    """
    statement = """
        SELECT d.id AS dataset_id,d.name AS dataset_name,v.id AS dataset_version_id,
               v.source_name,v.source_sha256,v.row_count,v.schema_json,
               cp.id AS canonical_profile_id,cp.spec_json,
               ip.id AS index_profile_id,ip.policy_json,
               ap.relative_path AS parquet_path,ac.relative_path AS canonical_path,
               (SELECT relative_path FROM artifacts x WHERE x.index_profile_id=ip.id
                AND x.kind='VALUE_ACCELERATION' LIMIT 1) AS value_path,
               (SELECT relative_path FROM artifacts x WHERE x.index_profile_id=ip.id
                AND x.kind='TOKEN_ACCELERATION' LIMIT 1) AS token_path
        FROM datasets d
        JOIN dataset_versions v ON v.id=d.active_version_id
        JOIN canonical_profiles cp ON cp.id=d.active_canonical_profile_id
        JOIN index_profiles ip ON ip.id=d.active_index_profile_id
        JOIN artifacts ap ON ap.dataset_version_id=v.id
          AND ap.kind IN ('RECORD_PARQUET','NORMALIZED_PARQUET')
        JOIN artifacts ac ON ac.canonical_profile_id=cp.id
          AND ac.kind IN ('CANONICAL_PARQUET','NORMALIZED_PARQUET')
    """
    parameters: tuple[str, ...] = ()
    if dataset is not None:
        resolution += " WHERE d.id=? OR d.name=?"
        statement += " WHERE d.id=? OR d.name=?"
        parameters = (dataset, dataset)
    resolution += " ORDER BY d.name"
    statement += " ORDER BY d.name,v.id"
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        active = [dict(row) for row in connection.execute(resolution, parameters)]
        if dataset is not None and not active:
            raise SearchFailure(f"dataset not found: {dataset}")
        for item in active:
            reason = None
            if item["active_version_id"] is None or item["version_id"] is None:
                reason = "active DatasetVersion is missing"
            elif item["version_status"] != "PUBLISHED":
                reason = f"active DatasetVersion has status {item['version_status']}"
            elif int(item["record_artifacts"] or 0) == 0:
                reason = "active DatasetVersion has no record artifact"
            elif item["canonical_profile_id"] is None:
                reason = "active canonical profile is missing"
            elif item["canonical_version_id"] != item["version_id"]:
                reason = "active canonical profile belongs to another DatasetVersion"
            elif item["canonical_status"] != "PUBLISHED":
                reason = f"active canonical profile has status {item['canonical_status']}"
            elif int(item["canonical_artifacts"] or 0) == 0:
                reason = "active canonical profile has no canonical artifact"
            elif item["index_profile_id"] is None:
                reason = "active acceleration profile is missing"
            elif item["index_version_id"] != item["version_id"]:
                reason = "active acceleration profile belongs to another DatasetVersion"
            elif item["index_canonical_id"] != item["canonical_profile_id"]:
                reason = "active acceleration profile belongs to another canonical profile"
            elif item["index_status"] != "PUBLISHED":
                reason = f"active acceleration profile has status {item['index_status']}"
            if reason is not None:
                raise SearchFailure(
                    f"workspace catalog cannot resolve active dataset "
                    f"{item['dataset_name']!r}: {reason}"
                )
        rows = [dict(row) for row in connection.execute(statement, parameters)]
    resolved = {str(row["dataset_id"]) for row in rows}
    expected = {str(row["dataset_id"]) for row in active}
    if resolved != expected:
        missing = sorted(
            str(row["dataset_name"]) for row in active if row["dataset_id"] not in resolved
        )
        raise SearchFailure(
            "workspace catalog could not resolve active search artifacts for: " + ", ".join(missing)
        )
    for row in rows:
        row["fields"] = _semantic_fields(row["schema_json"])
        row["canonical_fields"] = [
            CanonicalFieldSpec.from_dict(item) for item in json.loads(row["spec_json"])
        ]
        row["policies"] = parse_policy(row["policy_json"])
        for key in ("parquet_path", "canonical_path", "value_path", "token_path"):
            row[key.replace("_path", "_file")] = (
                workspace.resolve_relative(row[key]) if row.get(key) else None
            )
    return rows


def _supports_operator(field: QueryFieldContract, operator: SearchOperator) -> bool:
    return any(SearchOperator(item) is operator for item in field.supported_operators)


def _resolve_operator(
    clue: SearchClue, fields: Sequence[QueryFieldContract]
) -> SearchOperator:
    if clue.operator is not SearchOperator.AUTO:
        return SearchOperator(clue.operator)
    defaults = {field.default_operator for field in fields}
    if len(defaults) != 1 or None in defaults:
        raise SearchFailure(f"AUTO is ambiguous for semantic type {clue.semantic_type}")
    default = SearchOperator(defaults.pop())
    if (
        default is SearchOperator.EXACT
        and any(_supports_operator(field, SearchOperator.RANGE) for field in fields)
        and _YEAR.fullmatch(clue.value.strip())
    ):
        return SearchOperator.RANGE
    return default


def _prepare(clue: SearchClue, fields: Sequence[QueryFieldContract]) -> dict[str, Any]:
    operator = _resolve_operator(clue, fields)
    if not all(_supports_operator(field, operator) for field in fields):
        raise SearchFailure(f"{operator.value} is not supported for {clue.semantic_type}")
    spec = fields[0]
    if spec.canonicalizer_id is None or spec.canonicalizer_version is None:
        raise SearchFailure(f"semantic type {clue.semantic_type} is not searchable")
    if operator is SearchOperator.RANGE:
        value = clue.value.strip()
        year = _YEAR.fullmatch(value)
        if year:
            lower_year = int(year.group(1))
            upper_year = int(year.group(2) or year.group(1))
            if lower_year < 1 or upper_year > 9999 or lower_year > upper_year:
                raise SearchFailure("year range is invalid")
            lower, upper = f"{lower_year:04d}-01-01", f"{upper_year:04d}-12-31"
        else:
            pieces = value.split("..", 1)
            if len(pieces) != 2:
                raise SearchFailure(
                    "range value must be YYYY, YYYY..YYYY, or DATE..DATE"
                )
            normalized = [
                normalize_with(spec.canonicalizer_id, spec.canonicalizer_version, item).normalized
                for item in pieces
            ]
            if None in normalized or normalized[0] > normalized[1]:
                raise SearchFailure(
                    "range value is invalid; use complete dates and put the lower value first"
                )
            lower, upper = str(normalized[0]), str(normalized[1])
        return {"clue": clue, "operator": operator, "lower": lower, "upper": upper}
    normalized = normalize_with(
        spec.canonicalizer_id, spec.canonicalizer_version, clue.value
    ).normalized
    if normalized is None:
        expectation = _QUERY_VALUE_EXPECTATIONS.get(
            clue.semantic_type,
            "a non-empty value accepted by the semantic type's canonicalizer",
        )
        raise SearchFailure(
            f"query is not valid for {clue.semantic_type}; expected {expectation}"
        )
    result = {"clue": clue, "operator": operator, "lower": normalized, "upper": normalized}
    if operator is SearchOperator.TOKEN:
        result["tokens"] = tuple(sorted(set(normalized.split())))
    return result


def validate_search_clue(clue: SearchClue, contract: QueryFieldContract) -> None:
    """Validate one clue against the same grammar used by search execution."""
    _prepare(clue, (contract,))


def _primitive(operator: SearchOperator) -> AccelerationKind:
    return AccelerationKind.TOKEN if operator is SearchOperator.TOKEN else AccelerationKind.VALUE


def _rowset(
    segment: dict[str, Any],
    item: dict[str, Any],
    fields: list[int],
    *,
    limit: int = MAX_ROWSET_ORDINALS + 1,
) -> pa.UInt64Array:
    primitive = _primitive(item["operator"])
    path = segment[f"{primitive.value.lower()}_file"]
    if path is not None:
        if primitive is AccelerationKind.VALUE:
            return query_value_ordinals(path, fields, item["lower"], item["upper"], limit=limit)
        return query_token_ordinals(path, fields, item["tokens"], limit=limit)
    return pa.array([], type=pa.uint64())


def _rowset_cardinality(segment: dict[str, Any], item: dict[str, Any], fields: list[int]) -> int:
    primitive = _primitive(item["operator"])
    path = segment[f"{primitive.value.lower()}_file"]
    if path is not None:
        if primitive is AccelerationKind.VALUE:
            return count_value_ordinals(path, fields, item["lower"], item["upper"])
        return count_token_ordinals(path, fields, item["tokens"])
    return 0


def _projected_bytes(
    parquet: pq.ParquetFile, columns: set[str], row_groups: set[int] | None = None
) -> int:
    names = parquet.schema_arrow.names
    indices = {names.index(column) for column in columns}
    return sum(
        parquet.metadata.row_group(group).column(index).total_compressed_size
        for group in (range(parquet.num_row_groups) if row_groups is None else sorted(row_groups))
        for index in indices
    )


def _estimate_scan(
    workspace: Workspace,
    segment: dict[str, Any],
    prepared: list[dict[str, Any]],
    columns: set[str],
    *,
    row_groups: set[int] | None = None,
    evaluation_rows: int | None = None,
) -> dict[str, Any]:
    parquet = pq.ParquetFile(segment["canonical_file"])
    byte_count = _projected_bytes(parquet, columns | {"record_ordinal"}, row_groups=row_groups)
    row_count = int(segment["row_count"] or 0) if evaluation_rows is None else int(evaluation_rows)
    read_rates = workspace.catalog.performance_rates("SCAN_READ", 1)
    read_baseline = 150_000_000.0
    read_rate = (
        min(read_baseline, read_rates["bytes_per_second"])
        if read_rates and read_rates["sample_count"] < 3
        else read_rates["bytes_per_second"]
        if read_rates
        else read_baseline
    )
    seconds = byte_count / max(read_rate, 1.0)
    operators: dict[str, int] = {}
    operator_fields: dict[str, int] = {}
    for item in prepared:
        name = item["operator"].value
        compatible_fields = sum(
            field.semantic_type == item["clue"].semantic_type
            for field in segment["canonical_fields"]
        )
        evaluations = row_count * compatible_fields
        operators[name] = operators.get(name, 0) + evaluations
        operator_fields[name] = operator_fields.get(name, 0) + compatible_fields
    operator_rates = {
        name: workspace.catalog.performance_rates(f"SCAN_{name}", 1) for name in operators
    }
    for name, evaluations in operators.items():
        rates = operator_rates[name]
        baseline = 2_000_000.0 if name == "TOKEN" else 10_000_000.0
        rate = (
            min(baseline, rates["rows_per_second"])
            if rates and rates["sample_count"] < 3
            else rates["rows_per_second"]
            if rates
            else baseline
        )
        seconds += evaluations / max(rate, 1.0)
    sample_counts = [int(read_rates["sample_count"]) if read_rates else 0]
    sample_counts.extend(
        int(rates["sample_count"]) if rates else 0 for rates in operator_rates.values()
    )
    sample_count = min(sample_counts)
    observed_count = max(sample_counts)
    return {
        "row_count": row_count,
        "projected_bytes": byte_count,
        "estimated_seconds": (0.02 + seconds) * 1.25,
        "basis": (
            "local measurements"
            if sample_count >= 3
            else f"conservative baseline + {observed_count} local observation(s)"
            if observed_count
            else "conservative baseline"
        ),
        "confidence": "measured" if sample_count >= 3 else "bootstrap",
        "has_model": sample_count >= 3,
        "operator_row_evaluations": operators,
        "operator_field_counts": operator_fields,
    }


def _token_mask(values: pa.Array, tokens: tuple[str, ...]) -> pa.BooleanArray:
    lists = pc.utf8_split_whitespace(values)
    flat = pc.list_flatten(lists)
    parents = pc.list_parent_indices(lists)
    rows = pa.array(range(len(values)), type=pa.int64())
    result = pa.array([True] * len(values), type=pa.bool_())
    for token in tokens:
        matching = pc.filter(parents, pc.fill_null(pc.equal(flat, token), False))
        result = pc.and_(result, pc.is_in(rows, value_set=matching))
    return pc.fill_null(result, False)


def _field_mask(values: pa.Array, item: dict[str, Any]) -> pa.BooleanArray:
    if item["operator"] is SearchOperator.TOKEN:
        return _token_mask(values, item["tokens"])
    lower = pc.greater_equal(values, item["lower"])
    upper = pc.less_equal(values, item["upper"])
    return pc.fill_null(pc.and_(lower, upper), False)


def _scan_segment(
    segment: dict[str, Any],
    prepared: list[dict[str, Any]],
    candidates: pa.UInt64Array | None,
    limit: int,
    progress: Callable[[SearchProgress], None] | None = None,
) -> tuple[list[int], int, float]:
    relevant = {
        item["clue"].semantic_type: [
            field
            for field in segment["canonical_fields"]
            if field.semantic_type == item["clue"].semantic_type
        ]
        for item in prepared
    }
    columns = [
        "record_ordinal",
        *sorted({field.canonical_storage_name for fields in relevant.values() for field in fields}),
    ]
    parquet = pq.ParquetFile(segment["canonical_file"])
    found: list[int] = []
    scanned = 0
    started = time.perf_counter()
    if candidates is not None:
        starts = _row_group_starts(parquet)
        by_group: dict[int, list[tuple[int, int]]] = {}
        for ordinal in candidates.to_pylist():
            value = int(ordinal)
            group = bisect_right(starts, value) - 1
            by_group.setdefault(group, []).append((value, value - starts[group]))
        for group, positions in sorted(by_group.items()):
            table = parquet.read_row_group(group, columns=columns, use_threads=True)
            batch = pc.take(
                table,
                pa.array([position for _, position in positions], type=pa.int64()),
            )
            mask = pa.array([True] * batch.num_rows, type=pa.bool_())
            for item in prepared:
                clue_mask = pa.array([False] * batch.num_rows, type=pa.bool_())
                for field in relevant[item["clue"].semantic_type]:
                    clue_mask = pc.or_(
                        clue_mask, _field_mask(batch[field.canonical_storage_name], item)
                    )
                mask = pc.and_(mask, clue_mask)
            scanned += batch.num_rows
            if progress:
                progress(SearchProgress("SCANNING", scanned, len(candidates)))
            for ordinal in pc.filter(batch["record_ordinal"], mask).to_pylist():
                found.append(int(ordinal))
                if len(found) > limit:
                    return found, scanned, time.perf_counter() - started
        return found, scanned, time.perf_counter() - started
    for batch in parquet.iter_batches(columns=columns, batch_size=65_536, use_threads=True):
        mask = pa.array([True] * batch.num_rows, type=pa.bool_())
        for item in prepared:
            clue_mask = pa.array([False] * batch.num_rows, type=pa.bool_())
            for field in relevant[item["clue"].semantic_type]:
                clue_mask = pc.or_(
                    clue_mask, _field_mask(batch[field.canonical_storage_name], item)
                )
            mask = pc.and_(mask, clue_mask)
        scanned += batch.num_rows
        if progress:
            progress(SearchProgress("SCANNING", scanned, int(segment["row_count"] or 0)))
        for ordinal in pc.filter(batch["record_ordinal"], mask).to_pylist():
            found.append(int(ordinal))
            if len(found) > limit:
                return found, scanned, time.perf_counter() - started
    return found, scanned, time.perf_counter() - started


def _row_group_starts(parquet: pq.ParquetFile) -> list[int]:
    starts = [0]
    for group in range(parquet.num_row_groups):
        starts.append(starts[-1] + parquet.metadata.row_group(group).num_rows)
    return starts


def _materialize(
    segment: dict[str, Any],
    ordinals: list[int],
    prepared: list[dict[str, Any]],
    accesses: list[str],
) -> list[SearchCandidate]:
    original = pq.ParquetFile(segment["parquet_file"])
    canonical = pq.ParquetFile(segment["canonical_file"])
    starts = _row_group_starts(original)
    fields: list[SemanticFieldSpec] = segment["fields"]
    canonical_fields: list[CanonicalFieldSpec] = segment["canonical_fields"]
    by_group: dict[int, list[tuple[int, int]]] = {}
    for ordinal in ordinals:
        group = bisect_right(starts, ordinal) - 1
        by_group.setdefault(group, []).append((ordinal, ordinal - starts[group]))
    results = []
    for group, positions in sorted(by_group.items()):
        indices = pa.array([position for _, position in positions], type=pa.int64())
        original_table = pc.take(
            original.read_row_group(group, columns=[field.storage_name for field in fields]),
            indices,
        )
        canonical_table = pc.take(
            canonical.read_row_group(
                group, columns=[field.canonical_storage_name for field in canonical_fields]
            ),
            indices,
        )
        for offset, (ordinal, _) in enumerate(positions):
            original_by_id = {
                field.field_id: original_table[field.storage_name][offset].as_py()
                for field in fields
            }
            canonical_by_id = {
                field.field_id: canonical_table[field.canonical_storage_name][offset].as_py()
                for field in canonical_fields
            }
            record: dict[str, str | None] = {}
            seen: set[str] = set()
            for field in fields:
                name = (
                    field.source_name
                    if field.source_name not in seen
                    else f"{field.source_name}[{field.field_id}]"
                )
                seen.add(name)
                record[name] = original_by_id[field.field_id]
            matches = []
            for clue_index, (item, access) in enumerate(zip(prepared, accesses, strict=True)):
                provenances = []
                for field in canonical_fields:
                    if field.semantic_type != item["clue"].semantic_type:
                        continue
                    value = canonical_by_id[field.field_id]
                    if value is None or not _field_mask(pa.array([value]), item)[0].as_py():
                        continue
                    original_value = original_by_id[field.field_id]
                    if original_value is None:
                        continue
                    source_field = next(
                        value for value in fields if value.field_id == field.field_id
                    )
                    provenances.append(
                        Provenance(
                            segment["dataset_id"],
                            segment["dataset_name"],
                            segment["dataset_version_id"],
                            segment["source_sha256"],
                            segment["source_name"],
                            ordinal,
                            field.field_id,
                            source_field.source_name,
                            original_value,
                            value,
                            field.semantic_type,
                        )
                    )
                matches.append(
                    CandidateMatch(
                        clue_index,
                        item["clue"].semantic_type,
                        item["operator"],
                        access,
                        tuple(provenances),
                    )
                )
            results.append(
                SearchCandidate(
                    RecordRef(segment["dataset_version_id"], ordinal),
                    segment["dataset_id"],
                    segment["dataset_name"],
                    segment["dataset_version_id"],
                    segment["source_name"],
                    segment["source_sha256"],
                    tuple(matches),
                    record,
                    tuple(
                        {
                            "field_id": field.field_id,
                            "field_name": field.source_name,
                            "semantic_type": field.semantic_type,
                            "value": original_by_id[field.field_id],
                        }
                        for field in fields
                    ),
                )
            )
    return results


def _candidate_row_groups(segment: dict[str, Any], candidates: pa.UInt64Array) -> set[int]:
    starts = _row_group_starts(pq.ParquetFile(segment["canonical_file"]))
    return {bisect_right(starts, int(value)) - 1 for value in candidates.to_pylist()}


def _accelerated_rowsets(
    plan: dict[str, Any],
) -> tuple[list[tuple[dict[str, Any], pa.UInt64Array]], bool]:
    usable_count = sum(plan["accelerated"])
    usable = [
        (item, [field.field_id for field in fields])
        for item, fields, usable in zip(
            plan["prepared"], plan["clue_fields"], plan["accelerated"], strict=True
        )
        if usable
    ]
    if usable_count == 1 and len(plan["prepared"]) == 1:
        item, fields = usable[0]
        return [
            (
                item,
                _rowset(
                    plan["segment"],
                    item,
                    fields,
                    limit=int(plan["request_limit"]) + 1,
                ),
            )
        ], False
    ranked = [
        (_rowset_cardinality(plan["segment"], item, fields), item, fields)
        for item, fields in usable
    ]
    ranked.sort(
        key=lambda value: (
            value[0],
            value[1]["clue"].semantic_type,
            value[1]["operator"].value,
        )
    )
    if not ranked or ranked[0][0] > MAX_ROWSET_ORDINALS:
        return [], bool(ranked)
    rowsets: list[tuple[dict[str, Any], pa.UInt64Array]] = []
    candidates: pa.UInt64Array | None = None
    for cardinality, item, fields in ranked:
        if cardinality > MAX_ROWSET_ORDINALS:
            continue
        rowset = _rowset(plan["segment"], item, fields, limit=cardinality + 1)
        rowsets.append((item, rowset))
        candidates = rowset if candidates is None else intersect_rowsets(candidates, rowset)
        if len(candidates) <= _CANDIDATE_FILTER_ROWS:
            break
    return rowsets, False


def _choose_candidates(
    rowsets: list[tuple[dict[str, Any], pa.UInt64Array]],
    overflow: bool = False,
) -> tuple[pa.UInt64Array | None, list[dict[str, Any]], bool]:
    if not rowsets or overflow:
        return None, [], overflow
    candidates = rowsets[0][1]
    steps = [
        {
            "access": _primitive(rowsets[0][0]["operator"]).value,
            "semantic_type": rowsets[0][0]["clue"].semantic_type,
            "rows_after": len(candidates),
        }
    ]
    if len(candidates) > _CANDIDATE_FILTER_ROWS:
        for item, rowset in rowsets[1:]:
            before = len(candidates)
            candidates = intersect_rowsets(candidates, rowset)
            steps.append(
                {
                    "access": _primitive(item["operator"]).value,
                    "semantic_type": item["clue"].semantic_type,
                    "rows_before": before,
                    "rows_after": len(candidates),
                }
            )
            if len(candidates) <= _CANDIDATE_FILTER_ROWS:
                break
    return candidates, steps, False


def execute_search(
    workspace: Workspace,
    request: SearchRequest,
    *,
    progress: Callable[[SearchProgress], None] | None = None,
) -> SearchResult:
    if not request.clues:
        raise SearchFailure("at least one search clue is required")
    if not 1 <= request.limit <= 10_000:
        raise SearchFailure("limit must be between 1 and 10000")
    custom_types = workspace.catalog.list_custom_types()
    for clue in request.clues:
        try:
            contract = contract_for(clue.semantic_type, custom_types)
        except ValueError as error:
            raise SearchFailure(f"unknown semantic type: {clue.semantic_type}") from error
        if not contract.supported_operators:
            raise SearchFailure(f"semantic type {clue.semantic_type} is not searchable")
    reports: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    performance_samples: list[tuple[str, int, int, float]] = []
    retained_ordinals = 0
    with workspace.lock(exclusive=False):
        for segment in _segments(workspace, request.dataset):
            clue_fields = [
                [
                    field
                    for field in segment["canonical_fields"]
                    if field.semantic_type == clue.semantic_type
                ]
                for clue in request.clues
            ]
            if any(not fields for fields in clue_fields):
                reports.append(
                    {
                        "dataset_id": segment["dataset_id"],
                        "dataset_name": segment["dataset_name"],
                        "path": "not_applicable",
                        "missing_semantic_types": [
                            clue.semantic_type
                            for clue, fields in zip(request.clues, clue_fields, strict=True)
                            if not fields
                        ],
                    }
                )
                continue
            prepared = [
                _prepare(clue, fields)
                for clue, fields in zip(request.clues, clue_fields, strict=True)
            ]
            policy = {field.field_id: set(field.accelerations) for field in segment["policies"]}
            accelerated = []
            coverage = []
            for item, fields in zip(prepared, clue_fields, strict=True):
                primitive = _primitive(item["operator"])
                artifact = segment[f"{primitive.value.lower()}_file"] is not None
                covered = sum(primitive in policy.get(field.field_id, set()) for field in fields)
                complete = covered == len(fields)
                accelerated.append(complete and artifact)
                coverage.append(
                    "complete"
                    if complete and artifact
                    else "partial"
                    if covered and artifact
                    else "none"
                )
            columns = {field.canonical_storage_name for fields in clue_fields for field in fields}
            plan = {
                "segment": segment,
                "prepared": prepared,
                "clue_fields": clue_fields,
                "accelerated": accelerated,
                "coverage": coverage,
                "columns": columns,
                "request_limit": request.limit,
            }
            rowsets, overflow = _accelerated_rowsets(plan)
            candidates, steps, overflow = _choose_candidates(rowsets, overflow)
            full_estimate = _estimate_scan(workspace, segment, prepared, columns)
            chosen_estimate = full_estimate
            candidate_planned = False
            if candidates is not None:
                groups = _candidate_row_groups(segment, candidates)
                candidate_estimate = _estimate_scan(
                    workspace,
                    segment,
                    prepared,
                    columns,
                    row_groups=groups,
                    evaluation_rows=len(candidates),
                )
                if candidate_estimate["estimated_seconds"] < full_estimate["estimated_seconds"]:
                    chosen_estimate = candidate_estimate
                    candidate_planned = True
                else:
                    candidates = None
            recompute_candidates = False
            if candidates is not None:
                if retained_ordinals + len(candidates) > MAX_ROWSET_ORDINALS * 4:
                    candidates = None
                    recompute_candidates = True
                else:
                    retained_ordinals += len(candidates)
            plan.update(
                candidates=candidates,
                candidate_planned=candidate_planned,
                recompute_candidates=recompute_candidates,
                steps=steps,
                overflow=overflow,
                estimate=chosen_estimate,
                full_estimate=full_estimate,
            )
            plans.append(plan)

        bootstrap_permission = False
        for plan in plans:
            estimate = plan["estimate"]
            bootstrap = not estimate["has_model"] and (
                estimate["row_count"] > _BOOTSTRAP_ROWS
                or estimate["projected_bytes"] > _BOOTSTRAP_BYTES
            )
            bootstrap_permission = bootstrap_permission or bootstrap
        seconds = sum(plan["estimate"]["estimated_seconds"] for plan in plans)
        if (
            seconds > _SCAN_PERMISSION_SECONDS or bootstrap_permission
        ) and not request.allow_expensive_scan:
            rows = sum(plan["estimate"]["row_count"] for plan in plans)
            byte_count = sum(plan["estimate"]["projected_bytes"] for plan in plans)
            confidence = ", ".join(sorted({str(plan["estimate"]["confidence"]) for plan in plans}))
            raise SearchFailure(
                "search requires an estimated "
                f"{seconds:.1f}s scan of {rows:,} rows/{byte_count:,} bytes "
                f"({confidence}); rerun with --scan to allow it"
            )

        def run_plan(
            plan: dict[str, Any],
        ) -> tuple[list[int], dict[str, Any], list[tuple[str, int, int, float]], list[str]]:
            segment = plan["segment"]
            prepared = plan["prepared"]
            candidates = plan["candidates"]
            if plan["recompute_candidates"]:
                rowsets, overflow = _accelerated_rowsets(plan)
                candidates, _steps, _overflow = _choose_candidates(rowsets, overflow)
            started = time.perf_counter()
            ordinals, scanned, scan_seconds = _scan_segment(
                segment, prepared, candidates, request.limit, progress
            )
            planned_rows = max(int(plan["estimate"]["row_count"]), 1)
            processed_bytes = min(
                int(plan["estimate"]["projected_bytes"]),
                int(plan["estimate"]["projected_bytes"] * scanned / planned_rows),
            )
            samples = [
                (
                    "SCAN_READ",
                    scanned,
                    processed_bytes,
                    scan_seconds,
                )
            ]
            for item in prepared:
                field_count = sum(
                    field.semantic_type == item["clue"].semantic_type
                    for field in segment["canonical_fields"]
                )
                samples.append(
                    (
                        f"SCAN_{item['operator'].value}",
                        scanned * field_count,
                        0,
                        scan_seconds,
                    )
                )
            accesses = [
                _primitive(item["operator"]).value
                if usable
                else ("CANDIDATE_FILTER" if candidates is not None else "SCAN")
                for item, usable in zip(prepared, plan["accelerated"], strict=True)
            ]
            report = {
                "dataset_id": segment["dataset_id"],
                "dataset_name": segment["dataset_name"],
                "path": ("SCAN" if candidates is None else str(plan["steps"][0]["access"])),
                "rows_before": int(segment["row_count"] or 0),
                "candidate_rows": len(candidates) if candidates is not None else None,
                "scanned_rows": scanned,
                **plan["estimate"],
                "actual_seconds": time.perf_counter() - started,
                "scan_seconds": scan_seconds,
                "materialization_seconds": 0.0,
                "rowset_overflow": plan["overflow"],
                "steps": plan["steps"],
                "clues": [
                    {
                        "semantic_type": item["clue"].semantic_type,
                        "requested_operator": item["clue"].operator.value,
                        "resolved_operator": item["operator"].value,
                        "access": access,
                        "field_ids": [field.field_id for field in fields],
                        "complete_acceleration": usable,
                        "acceleration_coverage": coverage,
                    }
                    for item, fields, usable, coverage, access in zip(
                        prepared,
                        plan["clue_fields"],
                        plan["accelerated"],
                        plan["coverage"],
                        accesses,
                        strict=True,
                    )
                ],
            }
            plan["candidates"] = None
            return ordinals, report, samples, accesses

        if (
            progress is None
            and len(plans) > 1
            and not any(plan["recompute_candidates"] for plan in plans)
        ):
            with ThreadPoolExecutor(max_workers=min(4, len(plans))) as executor:
                outcomes = list(executor.map(run_plan, plans))
        else:
            outcomes = [run_plan(plan) for plan in plans]

        survivors: list[tuple[str, int, dict[str, Any]]] = []
        for plan, (ordinals, report, samples, accesses) in zip(plans, outcomes, strict=True):
            segment = plan["segment"]
            survivors.extend((segment["dataset_name"], ordinal, plan) for ordinal in ordinals)
            performance_samples.extend(samples)
            plan["report"] = report
            plan["accesses"] = accesses
            plan["candidates"] = None
            reports.append(report)

        survivors.sort(key=lambda item: (item[0], item[1]))
        truncated = len(survivors) > request.limit
        selected = survivors[: request.limit]
        records: list[SearchCandidate] = []
        for plan in plans:
            chosen = [ordinal for _, ordinal, owner in selected if owner is plan]
            if not chosen:
                continue
            started = time.perf_counter()
            records.extend(
                _materialize(plan["segment"], chosen, plan["prepared"], plan["accesses"])
            )
            plan["report"]["materialization_seconds"] = time.perf_counter() - started
    for workload, row_count, byte_count, elapsed in performance_samples:
        workspace.catalog.record_performance_sample(workload, 1, row_count, byte_count, elapsed)
    records.sort(key=lambda item: (item.dataset_name, item.ref.record_ordinal))
    execution = {
        "execution_version": 1,
        "segments": reports,
        "returned_record_count": len(records),
        "scanned_row_count": sum(int(item.get("scanned_rows", 0)) for item in reports),
        "result_truncated": truncated,
    }
    return SearchResult(request, tuple(records), truncated, execution)
