from __future__ import annotations

import os
import shutil
import sys
from dataclasses import replace
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import typer
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pandoracle import __version__
from pandoracle.acceleration_profiles import (
    AccelerationPlan,
    configure_acceleration,
    confirm_acceleration_plan,
    disable_acceleration,
    load_acceleration_plan,
    make_acceleration_plan,
    rebuild_acceleration,
    show_acceleration,
)
from pandoracle.cli_ui import (
    ChoiceKind,
    ImportProgressRenderer,
    PromptBackend,
    PromptChoice,
    QuestionaryPromptBackend,
    emit_schema_plan,
    review_schema_plan,
)
from pandoracle.config import (
    active_device_workspace,
    default_workspace_path,
    select_workspace,
    selected_workspace,
)
from pandoracle.contracts import contract_for
from pandoracle.device_host import (
    detection_enabled,
    disable_detection,
    enable_detection,
    run_watcher,
)
from pandoracle.device_models import (
    HostDevicePolicy,
    UnlockMode,
    load_host_devices,
)
from pandoracle.device_public import inspect_public_directory
from pandoracle.device_service import DeviceService
from pandoracle.device_udisks import ConnectedDevice, DriveCandidate
from pandoracle.errors import DeviceError, ImportFailure, PandoracleError, SearchFailure
from pandoracle.fs import write_json_atomic
from pandoracle.ingest import (
    MalformedRecord,
    MalformedRecordAction,
    MalformedRecordPolicy,
    RejectEscalation,
    analyze_csv,
    import_csv,
)
from pandoracle.maintenance import (
    GarbageCollectionPlan,
    GarbageCollectionResult,
    apply_gc,
    list_operations,
    plan_gc,
    recover,
    verify_workspace,
)
from pandoracle.models import (
    AccelerationKind,
    SearchCandidate,
    SearchClue,
    SearchOperator,
    SearchRequest,
    SemanticType,
)
from pandoracle.normalize import BUILTIN_TYPE_IDS, classify_query
from pandoracle.revision import analyze_dataset, revise_dataset
from pandoracle.schema import CustomTypeSpec, SchemaPlan, load_schema_document
from pandoracle.search import inspect_record, search
from pandoracle.universal_search import execute_search, validate_search_clue
from pandoracle.workspace import Workspace

app = typer.Typer(
    name="pandoracle",
    help=(
        "Import and search local datasets with complete source provenance. Run "
        "'pandoracle' for the recommended private interactive search experience."
    ),
    epilog=(
        "Examples: pandoracle; pandoracle init; pandoracle import FILE.csv. "
        "For automation use 'pandoracle search VALUE --format json'. The core remains "
        "path-based; use 'pandoracle device setup' for a supervised LUKS2 removable drive."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=False,
    pretty_exceptions_enable=False,
)
schema_app = typer.Typer(
    help=(
        "Prepare repeatable import schemas or correct an imported dataset. Type inference "
        "is advisory and every field must be confirmed."
    ),
    no_args_is_help=True,
    epilog=(
        "Example: pandoracle schema analyze contacts.csv --output contacts.schema.json"
    ),
)
acceleration_app = typer.Typer(
    help=(
        "Speed up frequent searches. Acceleration is optional and can be changed without "
        "changing records or their references."
    ),
    no_args_is_help=True,
    epilog="Example: pandoracle acceleration configure contacts",
)
types_app = typer.Typer(
    help="List, retire, or restore workspace custom semantic types.",
    invoke_without_command=True,
    epilog="Example: pandoracle types retire customer_code",
)
maintenance_app = typer.Typer(
    help="Verify, recover, inspect, or safely reclaim workspace state.",
    no_args_is_help=True,
    epilog="Example: pandoracle maintenance verify --format json",
)
device_app = typer.Typer(
    help="Provision and use encrypted Pandoracle removable drives.",
    no_args_is_help=True,
    epilog="Example: pandoracle device open",
)
detection_app = typer.Typer(
    help="Configure automatic recognition of trusted Pandoracle drives.",
    invoke_without_command=True,
    epilog="Example: pandoracle device detection enable",
)
app.add_typer(types_app, name="types")
app.add_typer(schema_app, name="schema")
app.add_typer(acceleration_app, name="acceleration")
app.add_typer(maintenance_app, name="maintenance")
app.add_typer(device_app, name="device")
device_app.add_typer(detection_app, name="detection")
console = Console()
error_console = Console(stderr=True)

WORKSPACE_HELP = (
    "Workspace override. By default Pandoracle uses an active Pandora device session, "
    "then the workspace selected by 'pandoracle init' or 'pandoracle workspace'."
)
FORMAT_HELP = (
    "Output format. Table is human-readable; JSON is machine-readable and disables prompts."
)


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"


def _acceleration_label(value: str) -> str:
    return {
        AccelerationKind.VALUE.value: "exact-value lookup",
        AccelerationKind.TOKEN.value: "whole-token lookup",
    }[value]


def _search_access_label(value: str) -> str:
    if value in {item.value for item in AccelerationKind}:
        return _acceleration_label(value)
    return value.replace("_", " ").lower()


def _workspace_path(value: Path | None) -> Path:
    if value is not None:
        return value
    configured = os.environ.get("PANDORACLE_WORKSPACE")
    if configured:
        return Path(configured)
    active = active_device_workspace()
    if active is not None:
        return active
    selected = selected_workspace()
    if selected is not None:
        return selected
    raise PandoracleError(
        "no workspace is selected; run 'pandoracle init' or 'pandoracle workspace PATH'"
    )


def _read_brand_asset(name: str) -> str:
    return files("pandoracle").joinpath("assets", name).read_text(encoding="utf-8").rstrip()


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and console.is_terminal


def _emit_brand_asset(name: str, *, style: str, width: int) -> None:
    asset = Text(_read_brand_asset(name), style=style)
    console.print(Align.center(asset, width=width, pad=False))


def _emit_brand(*, width: int | None = None) -> None:
    width = width or shutil.get_terminal_size(fallback=(80, 24)).columns
    if width >= 60:
        _emit_brand_asset("logo.txt", style="cyan", width=width)
        _emit_brand_asset("wordmark.txt", style="bold cyan", width=width)
    elif width >= 42:
        _emit_brand_asset("logo-compact.txt", style="cyan", width=width)
        name = Text("PANDORACLE", style="bold cyan")
        console.print(Align.center(name, width=width, pad=False))
    else:
        console.print("PANDORACLE", style="bold cyan")
    tagline = Text("hunt through private data", style="dim")
    console.print(Align.center(tagline, width=width, pad=False) if width >= 42 else tagline)
    console.print()


def _emit_json(value: Any) -> None:
    console.print_json(data=value)


def _emit_datasets(sources: list[dict[str, Any]]) -> None:
    table = Table(title="Datasets")
    table.add_column("Dataset")
    table.add_column("Rows", justify="right")
    table.add_column("Source")
    table.add_column("Semantic fields")
    table.add_column("Search speed")
    for source in sources:
        policies = {
            int(item["field_id"]): item["accelerations"]
            for item in source["index_policies"]
        }
        accelerated = ", ".join(
            f"{field['source_name']}:"
            + "+".join(
                _acceleration_label(item) for item in policies[int(field["field_id"])]
            )
            for field in source["schema"]
            if policies.get(int(field["field_id"]))
        )
        semantic = ", ".join(
            f"{field['source_name']}:{field['semantic_type']}" for field in source["schema"]
        )
        table.add_row(
            source["name"],
            str(source["row_count"] or 0),
            source["source_name"] or "—",
            semantic or "—",
            accelerated or "scan (acceleration optional)",
        )
    console.print(table)


def _emit_gc(value: GarbageCollectionPlan | GarbageCollectionResult) -> None:
    table = Table(title="Garbage collection")
    table.add_column("Kind")
    table.add_column("Reason")
    table.add_column("Target")
    table.add_column("Bytes", justify="right")
    for item in value.items:
        table.add_row(
            item.kind,
            item.reason,
            item.relative_path or item.catalog_id or "—",
            f"{item.size_bytes:,}" if item.relative_path is not None else "—",
        )
    console.print(table)
    mode = "Applied" if isinstance(value, GarbageCollectionResult) else "Dry run"
    byte_count = (
        value.reclaimed_bytes
        if isinstance(value, GarbageCollectionResult)
        else value.reclaimable_bytes
    )
    console.print(f"{mode}: {len(value.items)} item(s), {byte_count:,} reclaimable bytes")
    for blocker in value.blockers:
        error_console.print(f"Blocked: {blocker}")


def _parse_query_clue(value: str) -> SearchClue:
    if "=" not in value:
        raise PandoracleError("clue must be TYPE[:OPERATOR]=VALUE")
    descriptor, clue_value = value.split("=", 1)
    if not descriptor or not clue_value:
        raise PandoracleError("clue must include a semantic type and value")
    if ":" in descriptor:
        semantic_type, operator_text = descriptor.split(":", 1)
    else:
        semantic_type, operator_text = descriptor, SearchOperator.AUTO.value
    if operator_text.upper() == "YEAR":
        operator_text = SearchOperator.RANGE.value
    try:
        operator = SearchOperator(operator_text.upper())
    except ValueError as error:
        raise PandoracleError(f"unknown search operator: {operator_text}") from error
    identifier = (
        semantic_type.upper() if semantic_type.upper() in BUILTIN_TYPE_IDS else semantic_type
    )
    return SearchClue(identifier, clue_value, operator)


def _emit_search_result(result: Any, *, explain: bool) -> None:
    _emit_candidates(list(result.records), result.execution)
    if len(result.records) == 1:
        console.print(f"Full record: pandoracle inspect '{result.records[0].ref.external_id}'")
    elif result.records:
        console.print(
            "Full records are included in JSON output; inspect any RecordRef for a table view."
        )
    if explain:
        table = Table(title="Execution")
        table.add_column("Dataset")
        table.add_column("Path")
        table.add_column("Candidates", justify="right")
        table.add_column("Projected")
        table.add_column("Estimated")
        table.add_column("Actual")
        for segment in result.execution["segments"]:
            if segment.get("path") == "not_applicable":
                continue
            table.add_row(
                segment["dataset_name"],
                _search_access_label(str(segment["path"])),
                "—" if segment.get("candidate_rows") is None else f"{segment['candidate_rows']:,}",
                f"{segment.get('projected_bytes', 0):,} B",
                f"{segment.get('estimated_seconds', 0):.2f}s",
                f"{segment.get('actual_seconds', 0):.2f}s",
            )
        console.print(table)


def _emit_acceleration_plan(plan: AccelerationPlan) -> None:
    selected = {
        (field.field_id, primitive) for field in plan.fields for primitive in field.accelerations
    }
    table = Table(title="Acceleration plan")
    table.add_column("Field", justify="right")
    table.add_column("Type")
    table.add_column("Capability")
    table.add_column("Utility")
    table.add_column("Selected")
    table.add_column("Size")
    table.add_column("Build")
    types = {field.field_id: field.semantic_type for field in plan.fields}
    for estimate in plan.estimates:
        label = (
            "Whole-token lookup"
            if estimate.primitive is AccelerationKind.TOKEN
            else "Exact-value and range lookup"
        )
        table.add_row(
            str(estimate.field_id),
            types[estimate.field_id],
            label,
            estimate.utility_tier,
            "Yes" if (estimate.field_id, estimate.primitive) in selected else "No",
            f"{estimate.size_bytes / 1024**2:.1f} MiB",
            f"~{estimate.seconds:.1f}s",
        )
    chosen = [item for item in plan.estimates if (item.field_id, item.primitive) in selected]
    selected_read_seconds = sum(
        max(
            (item.read_seconds for item in chosen if item.field_id == field_id),
            default=0.0,
        )
        for field_id in {item.field_id for item in chosen}
    )
    selected_seconds = selected_read_seconds + sum(
        item.processing_seconds + item.finalization_seconds for item in chosen
    )
    table.caption = (
        f"Selected: +{sum(item.size_bytes for item in chosen) / 1024**2:.1f} MiB, "
        f"~{selected_seconds:.1f}s"
    )
    console.print(table)


def _review_acceleration_plan(plan: AccelerationPlan) -> AccelerationPlan:
    selections = {field.field_id: list(field.accelerations) for field in plan.fields}
    prompts = QuestionaryPromptBackend()
    while True:
        current = confirm_acceleration_plan(
            plan, {field_id: tuple(values) for field_id, values in selections.items()}
        )
        _emit_acceleration_plan(current)
        choices = [
            PromptChoice(
                "Confirm acceleration profile", "__confirm__", ChoiceKind.RECOMMENDED
            )
        ]
        choices.extend(
            PromptChoice(
                f"#{item.field_id} {_acceleration_label(item.primitive.value)} (selected)"
                if item.primitive in selections[item.field_id]
                else f"#{item.field_id} {_acceleration_label(item.primitive.value)} (not selected)",
                f"{item.field_id}:{item.primitive.value}",
            )
            for item in plan.estimates
        )
        choices.append(PromptChoice("Cancel", "__cancel__", ChoiceKind.DESTRUCTIVE))
        action = prompts.select("Acceleration action", choices, default="__confirm__")
        if action in {None, "__cancel__"}:
            raise ImportFailure("acceleration review cancelled")
        if action == "__confirm__":
            return current
        field_text, primitive_text = action.split(":", 1)
        field_id = int(field_text)
        primitive = AccelerationKind(primitive_text)
        if primitive in selections[field_id]:
            selections[field_id].remove(primitive)
        else:
            selections[field_id].append(primitive)


def _candidate_title(candidate: SearchCandidate) -> tuple[str, int | None]:
    for field in candidate.fields:
        if field["semantic_type"] == SemanticType.PERSON_NAME.value and field["value"]:
            return str(field["value"]), int(field["field_id"])
    for match in candidate.matches:
        if match.provenances:
            provenance = match.provenances[0]
            return provenance.original_value, provenance.field_id
    return candidate.ref.external_id, None


def _emit_candidates(
    candidates: list[SearchCandidate], execution: dict[str, Any], *, offset: int = 0
) -> None:
    truncated = bool(execution.get("result_truncated"))
    total = int(execution.get("returned_candidate_count", len(candidates)))
    count_text = f"{total}+" if truncated else str(total)
    table = Table(title=f"Candidates ({count_text})")
    table.add_column("#", justify="right")
    table.add_column("Candidate")
    table.add_column("Useful fields")
    table.add_column("Dataset")
    table.add_column("Matched")
    table.add_column("Record reference")
    priority = {
        SemanticType.DATE_OF_BIRTH.value: 0,
        SemanticType.DATE.value: 1,
        SemanticType.PHONE.value: 2,
        SemanticType.EMAIL.value: 3,
    }
    for local_index, candidate in enumerate(candidates, start=offset + 1):
        title, title_field_id = _candidate_title(candidate)
        matched_ids = {
            provenance.field_id for match in candidate.matches for provenance in match.provenances
        }
        details = [
            field
            for field in candidate.fields
            if field["value"] not in {None, ""} and field["field_id"] != title_field_id
        ]
        details.sort(
            key=lambda field: (
                0 if field["field_id"] in matched_ids else 1,
                priority.get(str(field["semantic_type"]), 10),
                int(field["field_id"]),
            )
        )
        detail_text = (
            "\n".join(f"{field['field_name']}: {field['value']}" for field in details[:4]) or "—"
        )
        matched_text = "\n".join(
            f"{match.semantic_type}:{match.mode.value.lower()} "
            f"({_search_access_label(match.access)})"
            for match in candidate.matches
        )
        table.add_row(
            str(local_index),
            title,
            detail_text,
            candidate.dataset_name,
            matched_text,
            candidate.ref.external_id,
        )
    console.print(table)
    if truncated:
        console.print("Warning: results were truncated; refinement only covers shown candidates.")
    console.print(f"Rows examined: {execution.get('scanned_row_count', 0):,}")


def _emit_inspected_record(value: dict[str, Any]) -> None:
    console.print(f"Record: {value['record_ref']}")
    console.print(f"Dataset: {value['dataset_name']} ({value['dataset_id']})")
    console.print(f"Source: {value['source_name']}")
    console.print(f"Source SHA-256: {value['source_sha256']}")
    table = Table(title="Original record")
    table.add_column("Field")
    table.add_column("Semantic type")
    table.add_column("Value")
    for field in value.get("fields", []):
        table.add_row(
            field["field_name"],
            field["semantic_type"],
            "—" if field["value"] is None else str(field["value"]),
        )
    console.print(table)


def _query_type_choices(workspace: Workspace, dataset: str | None) -> list[PromptChoice]:
    identifiers: set[str] = set()
    for source in workspace.catalog.list_sources():
        if dataset is not None and dataset not in {source["dataset_id"], source["name"]}:
            continue
        identifiers.update(
            field["semantic_type"]
            for field in source["schema"]
            if field["semantic_type"] != SemanticType.UNKNOWN.value
        )
    return [PromptChoice(identifier, identifier) for identifier in sorted(identifiers)]


def _emit_shell_help() -> None:
    console.print(
        "Enter one value for a quick search. Pandoracle infers one semantic type and "
        "searches fields tagged with that type in every active dataset."
    )
    console.print(
        "AUTO matching is exact for email, phone, IP, domain, and username. A complete "
        "date is exact, a year is a range, and a person name requires all whole tokens."
    )
    console.print(
        "Use :search to choose the dataset, semantic type, operator, or multiple clues."
    )
    console.print(
        "Import and maintenance are ordinary pandoracle commands, not shell commands. "
        "Run them in another terminal while this session remains open."
    )
    console.print("Commands: :search, :datasets, :types, :help, :quit")


def _emit_shell_auto_plan(semantic_type: SemanticType) -> None:
    operator = contract_for(semantic_type).default_operator
    if operator is None:
        raise SearchFailure(f"semantic type {semantic_type.value} is not searchable")
    console.print(
        f"Quick search: inferred {semantic_type.value}; AUTO -> {operator.value}; "
        "scope: all active datasets."
    )


def _prompt_clue(
    workspace: Workspace, dataset: str | None, prompts: PromptBackend
) -> SearchClue | None:
    semantic_type = prompts.select(
        "Semantic type",
        _query_type_choices(workspace, dataset)
        + [PromptChoice("Cancel", "__cancel__", ChoiceKind.DESTRUCTIVE)],
    )
    if semantic_type in {None, "__cancel__"}:
        return None
    contract = contract_for(semantic_type, workspace.catalog.list_custom_types())
    automatic_label = "Automatic"
    labels = {
        SearchOperator.EXACT: "Exact value",
        SearchOperator.TOKEN: "Whole tokens",
        SearchOperator.RANGE: "Value or year range",
    }
    if semantic_type in {
        SemanticType.DATE.value,
        SemanticType.DATE_OF_BIRTH.value,
    }:
        automatic_label = "Automatic (complete date -> EXACT; year -> RANGE)"
        labels[SearchOperator.EXACT] = "Exact date (YYYY-MM-DD, DD.MM.YYYY, or DD/MM/YYYY)"
        labels[SearchOperator.RANGE] = "Date or year range (YYYY, YYYY..YYYY, or DATE..DATE)"
    operators = [
        PromptChoice(automatic_label, SearchOperator.AUTO.value, ChoiceKind.RECOMMENDED)
    ]
    operators.extend(
        PromptChoice(labels[item], item.value) for item in contract.supported_operators
    )
    operator = prompts.select("Operator", operators, default=SearchOperator.AUTO.value)
    if operator is None:
        return None

    def validate(value: str) -> bool | str:
        if not value.strip():
            return "Enter a value."
        try:
            validate_search_clue(SearchClue(semantic_type, value, operator), contract)
        except SearchFailure as error:
            return str(error)
        return True

    value = prompts.text("Clue value", validate=validate)
    if value is None or not value.strip():
        return None
    return SearchClue(semantic_type, value, operator)


def _prompt_candidate_query(workspace: Workspace, prompts: PromptBackend) -> SearchRequest | None:
    sources = workspace.catalog.list_sources()
    dataset = prompts.select(
        "Dataset scope",
        [PromptChoice("All active datasets", "__all__", ChoiceKind.RECOMMENDED)]
        + [PromptChoice(item["name"], item["dataset_id"]) for item in sources]
        + [PromptChoice("Cancel", "__cancel__", ChoiceKind.DESTRUCTIVE)],
        default="__all__",
    )
    if dataset in {None, "__cancel__"}:
        return None
    selected_dataset = None if dataset == "__all__" else dataset
    clues: list[SearchClue] = []
    while True:
        clue = _prompt_clue(workspace, selected_dataset, prompts)
        if clue is None:
            return None
        clues.append(clue)
        action = prompts.select(
            "Query",
            [
                PromptChoice("Run search", "run", ChoiceKind.RECOMMENDED),
                PromptChoice("Add another clue", "add", ChoiceKind.UTILITY),
                PromptChoice("Cancel", "cancel", ChoiceKind.DESTRUCTIVE),
            ],
            default="run",
        )
        if action == "run":
            return SearchRequest(tuple(clues), dataset=selected_dataset)
        if action != "add":
            return None


def _run_shell_candidate_query(
    workspace: Workspace, query: SearchRequest, prompts: PromptBackend
) -> tuple[list[SearchCandidate], dict[str, Any]] | None:
    try:
        result = search(workspace, query)
        return list(result.records), result.execution
    except SearchFailure as error:
        if "rerun with --scan" not in str(error):
            raise
        console.print(str(error))
        action = prompts.select(
            "Expensive scan",
            [
                PromptChoice("Allow the planned scan", "scan", ChoiceKind.UTILITY),
                PromptChoice("Cancel", "cancel", ChoiceKind.DESTRUCTIVE),
            ],
            default="scan",
        )
        if action != "scan":
            return None
        scanned = SearchRequest(
            query.clues,
            dataset=query.dataset,
            limit=query.limit,
            allow_expensive_scan=True,
        )
        result = search(workspace, scanned)
        return list(result.records), result.execution


def _candidate_shell_session(
    workspace: Workspace,
    initial_query: SearchRequest,
    initial_candidates: list[SearchCandidate],
    initial_execution: dict[str, Any],
    prompts: PromptBackend,
) -> None:
    states: list[tuple[SearchRequest, list[SearchCandidate], dict[str, Any], int]] = [
        (initial_query, initial_candidates, initial_execution, 0)
    ]
    while states:
        query, candidates, execution, page = states[-1]
        start = page * 10
        shown = candidates[start : start + 10]
        _emit_candidates(shown, execution, offset=start)
        choices = []
        if candidates:
            choices.extend(
                (
                    PromptChoice("Refine current candidates", "refine", ChoiceKind.UTILITY),
                    PromptChoice("Open a candidate", "open", ChoiceKind.UTILITY),
                )
            )
        if start + 10 < len(candidates):
            choices.append(PromptChoice("Next page", "next", ChoiceKind.NAVIGATION))
        if page:
            choices.append(PromptChoice("Previous page", "previous", ChoiceKind.NAVIGATION))
        if len(states) > 1:
            choices.append(
                PromptChoice("Back to previous result set", "back", ChoiceKind.NAVIGATION)
            )
        choices.extend(
            (
                PromptChoice("New search", "new", ChoiceKind.UTILITY),
                PromptChoice("Quit", "quit", ChoiceKind.DESTRUCTIVE),
            )
        )
        action = prompts.select(
            "Candidate actions",
            choices,
            default="refine" if candidates else "new",
        )
        if action in {None, "new"}:
            return
        if action == "quit":
            raise EOFError
        if action == "next":
            states[-1] = (query, candidates, execution, page + 1)
            continue
        if action == "previous":
            states[-1] = (query, candidates, execution, page - 1)
            continue
        if action == "back":
            states.pop()
            continue
        if action == "open":
            if not shown:
                console.print("No candidates to open.")
                continue
            selected = prompts.select(
                "Open candidate",
                [
                    PromptChoice(
                        f"{start + index + 1}. {_candidate_title(candidate)[0]}",
                        candidate.ref.external_id,
                    )
                    for index, candidate in enumerate(shown)
                ]
                + [PromptChoice("Back", "__back__", ChoiceKind.NAVIGATION)],
            )
            if selected and selected != "__back__":
                _emit_inspected_record(inspect_record(workspace, selected, include_fields=True))
                prompts.select(
                    "Record",
                    [PromptChoice("Back to candidates", "back", ChoiceKind.NAVIGATION)],
                )
            continue
        if action == "refine":
            clue = _prompt_clue(workspace, query.dataset, prompts)
            if clue is None:
                continue
            refined_query = SearchRequest(
                (*query.clues, clue),
                dataset=query.dataset,
                limit=query.limit,
            )
            result = search(workspace, refined_query)
            states.append((refined_query, list(result.records), result.execution, 0))


def _emit_schema_plan(plan: SchemaPlan) -> None:
    emit_schema_plan(plan, console)


def _review_schema_plan(
    plan: SchemaPlan,
    existing_custom: dict[str, CustomTypeSpec],
    *,
    prompts: PromptBackend | None = None,
    workspace: Workspace | None = None,
) -> SchemaPlan:
    unavailable_type_ids: set[str] = set()
    if workspace is not None:
        unavailable_type_ids = set(workspace.catalog.list_custom_types()) - set(
            existing_custom
        )
    return review_schema_plan(
        plan,
        existing_custom,
        console=console,
        prompts=prompts,
        unavailable_type_ids=unavailable_type_ids,
    )


def _resolve_cli_schema(
    draft: SchemaPlan,
    schema_path: Path | None,
    *,
    interactive: bool,
    existing_custom: dict[str, CustomTypeSpec],
    workspace: Workspace | None = None,
) -> SchemaPlan:
    if schema_path is None:
        if not interactive:
            raise ImportFailure(
                "non-interactive import/revision requires a fully confirmed --schema file"
            )
        return _review_schema_plan(draft, existing_custom, workspace=workspace)
    document = load_schema_document(schema_path)
    plan = document
    if not plan.confirmed:
        if not interactive:
            raise ImportFailure("schema plan is not fully confirmed")
        return _review_schema_plan(plan, existing_custom, workspace=workspace)
    return plan


def _prompt_malformed_record(
    record: MalformedRecord,
    prompts: PromptBackend,
    progress: ImportProgressRenderer,
) -> MalformedRecordAction:
    actual = "unavailable (parser error)" if record.actual_fields is None else str(
        record.actual_fields
    )
    with progress.paused():
        error_console.print(f"Malformed CSV {record.position}")
        error_console.print(f"Expected fields: {record.expected_fields}; actual: {actual}")
        error_console.print(f"Reason: {record.reason}")
        while True:
            action = prompts.select(
                "Malformed record",
                [
                    PromptChoice(
                        "Quarantine this record and continue",
                        MalformedRecordAction.QUARANTINE.value,
                        ChoiceKind.RECOMMENDED,
                    ),
                    PromptChoice(
                        "Quarantine all similar records and continue",
                        MalformedRecordAction.QUARANTINE_SIMILAR.value,
                        ChoiceKind.UTILITY,
                    ),
                    PromptChoice("Inspect rejected record", "inspect", ChoiceKind.UTILITY),
                    PromptChoice(
                        "Abort import",
                        MalformedRecordAction.ABORT.value,
                        ChoiceKind.DESTRUCTIVE,
                    ),
                ],
                default=MalformedRecordAction.QUARANTINE.value,
            )
            if action == "inspect":
                error_console.print(
                    Panel(
                        Text(record.raw_record.rstrip("\r\n") or "(empty record)"),
                        title=f"Rejected {record.position}",
                        border_style="cyan",
                    )
                )
                continue
            if action is None:
                return MalformedRecordAction.ABORT
            return MalformedRecordAction(action)


def _prompt_reject_escalation(
    escalation: RejectEscalation,
    prompts: PromptBackend,
    progress: ImportProgressRenderer,
) -> bool:
    with progress.paused():
        error_console.print(
            "Reject safety check: "
            f"{escalation.rejected_count:,} of {escalation.processed_count:,} records "
            f"({escalation.reject_rate:.1%}) are quarantined. This may indicate an incorrect "
            "delimiter, encoding, or parser configuration."
        )
        action = prompts.select(
            "Reject safety check",
            [
                PromptChoice("Abort and review import settings", "abort", ChoiceKind.DESTRUCTIVE),
                PromptChoice("Continue quarantining", "continue", ChoiceKind.UTILITY),
            ],
            default="abort",
        )
    return action == "continue"


@schema_app.command(
    "analyze",
    epilog=("Example: pandoracle schema analyze source.csv --output source.schema.json"),
)
def schema_analyze_command(
    source: Annotated[Path, typer.Argument(help="CSV source file")],
    output: Annotated[Path, typer.Option("--output", help="Draft schema plan JSON")],
    encoding: Annotated[
        str,
        typer.Option("--encoding", help="Character encoding used to decode the CSV source."),
    ] = "utf-8",
) -> None:
    """Analyze a CSV and write an unconfirmed, sample-free schema plan."""
    if output.expanduser().absolute() == source.expanduser().absolute():
        raise ImportFailure("schema output must not overwrite the source file")
    plan = analyze_csv(source, encoding=encoding)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output, plan.to_dict())
    _emit_schema_plan(plan)
    console.print(f"Draft schema plan: {output}")
    console.print(f"Confirm it with: pandoracle schema review '{output}'")


@schema_app.command(
    "review",
    epilog=(
        "Use arrows or j/k to select a column, Enter to choose, and Ctrl-C to cancel. "
        "Example: pandoracle schema review source.schema.json"
    ),
)
def schema_review_command(
    plan_path: Annotated[Path, typer.Argument(help="Schema plan JSON")],
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
) -> None:
    """Interactively edit and confirm a schema plan in place."""
    document = load_schema_document(plan_path)
    if not isinstance(document, SchemaPlan):
        raise ImportFailure("schema review requires a versioned schema plan")
    workspace = Workspace.open(_workspace_path(workspace_path))
    custom_types = workspace.catalog.list_custom_types(include_retired=False)
    confirmed = _review_schema_plan(document, custom_types, workspace=workspace)
    write_json_atomic(plan_path, confirmed.to_dict())
    console.print(f"Confirmed schema plan: {plan_path}")


@acceleration_app.command(
    "show", epilog="Example: pandoracle acceleration show contacts"
)
def acceleration_show_command(
    dataset: Annotated[str, typer.Argument(help="Logical dataset name or UUID.")],
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    history: Annotated[
        bool, typer.Option("--history", help="Include superseded and retired profiles.")
    ] = False,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Show current acceleration or its earlier configurations."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    profiles = show_acceleration(workspace, dataset, history=history)
    if output_format is OutputFormat.JSON:
        _emit_json(profiles)
        return
    table = Table(title="Acceleration profiles")
    table.add_column("Profile")
    table.add_column("Status")
    table.add_column("Policy")
    table.add_column("Postings", justify="right")
    table.add_column("Size", justify="right")
    for profile in profiles:
        enabled = [
            f"#{field['field_id']}: {_acceleration_label(primitive)}"
            for field in profile["policy"]
            for primitive in field.get("accelerations", [])
        ]
        table.add_row(
            str(profile["id"])[:12],
            str(profile["status"]),
            ", ".join(enabled) or "scan-only",
            f"{int(profile['posting_count']):,}",
            f"{int(profile['artifact_bytes']) / 1024**2:.1f} MiB",
        )
    console.print(table)


@acceleration_app.command(
    "plan", epilog="Example: pandoracle acceleration plan contacts --output plan.json"
)
def acceleration_plan_command(
    dataset: Annotated[str, typer.Argument(help="Logical dataset name or UUID.")],
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="Write the reusable plan JSON here.")
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Estimate useful exact-value and whole-token acceleration."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    plan = make_acceleration_plan(workspace, dataset)
    if output is not None:
        write_json_atomic(output, plan.to_dict())
    if output_format is OutputFormat.JSON:
        _emit_json(plan.to_dict())
    else:
        _emit_acceleration_plan(plan)
        if output is not None:
            console.print(f"Plan written: {output}")


@acceleration_app.command(
    "configure", epilog="Example: pandoracle acceleration configure contacts"
)
def acceleration_configure_command(
    dataset: Annotated[str, typer.Argument(help="Logical dataset name or UUID.")],
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    plan_path: Annotated[
        Path | None, typer.Option("--plan", help="Confirmed acceleration plan JSON.")
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Build and activate the selected search acceleration."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    plan = (
        load_acceleration_plan(plan_path)
        if plan_path
        else make_acceleration_plan(workspace, dataset)
    )
    if not plan.confirmed:
        if output_format is OutputFormat.JSON or not sys.stdin.isatty():
            raise ImportFailure(
                "non-interactive acceleration configuration requires a confirmed --plan"
            )
        plan = _review_acceleration_plan(plan)
    if output_format is OutputFormat.TABLE:
        with ImportProgressRenderer(error_console) as display:
            result = configure_acceleration(workspace, dataset, plan, progress=display.update)
    else:
        result = configure_acceleration(workspace, dataset, plan)
    if output_format is OutputFormat.JSON:
        _emit_json(result.to_dict())
    else:
        action = "Reused" if result.deduplicated else "Published"
        console.print(f"{action} acceleration profile {result.index_profile_id}")
        console.print(
            f"DatasetVersion preserved: {result.dataset_version_id}; "
            f"postings: {result.posting_count:,}; size: {result.artifact_bytes:,} bytes"
        )


@acceleration_app.command(
    "rebuild", epilog="Example: pandoracle acceleration rebuild contacts"
)
def acceleration_rebuild_command(
    dataset: Annotated[str, typer.Argument(help="Logical dataset name or UUID.")],
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Force a fresh generation with the active policy."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    if output_format is OutputFormat.TABLE:
        with ImportProgressRenderer(error_console) as display:
            result = rebuild_acceleration(workspace, dataset, progress=display.update)
    else:
        result = rebuild_acceleration(workspace, dataset)
    _emit_json(result.to_dict()) if output_format is OutputFormat.JSON else console.print(
        f"Published acceleration profile {result.index_profile_id}"
    )


@acceleration_app.command(
    "disable", epilog="Example: pandoracle acceleration disable contacts"
)
def acceleration_disable_command(
    dataset: Annotated[str, typer.Argument(help="Logical dataset name or UUID.")],
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Turn acceleration off; the dataset remains searchable by scanning."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    result = disable_acceleration(workspace, dataset)
    if output_format is OutputFormat.JSON:
        _emit_json(result.to_dict())
    else:
        console.print(f"Acceleration disabled; active profile {result.index_profile_id}")


def _emit_semantic_types(workspace: Workspace) -> None:
    table = Table(title="Semantic types")
    table.add_column("Type ID")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("AUTO")
    table.add_column("Label")
    for identifier in sorted(BUILTIN_TYPE_IDS):
        from pandoracle.contracts import builtin_contract

        contract = builtin_contract(identifier)
        table.add_row(
            identifier,
            "built-in",
            "active",
            contract.default_operator.value if contract.default_operator else "—",
            identifier,
        )
    definitions = workspace.catalog.list_custom_types()
    for row in workspace.catalog.list_custom_type_rows():
        definition = definitions[str(row["type_id"])]
        table.add_row(
            definition.type_id,
            "custom",
            "retired" if row["retired_at"] else "active",
            definition.default_operator,
            definition.label,
        )
    console.print(table)


@types_app.callback(invoke_without_command=True)
def types_command(
    ctx: typer.Context,
    workspace_path: Annotated[
        Path | None, typer.Option("--workspace", help=WORKSPACE_HELP)
    ] = None,
) -> None:
    """Show semantic types, or use a subcommand to change a custom type."""
    if ctx.invoked_subcommand is None:
        _emit_semantic_types(Workspace.open(_workspace_path(workspace_path)))


@types_app.command(
    "retire", epilog="Example: pandoracle types retire customer_code"
)
def types_retire_command(
    type_id: Annotated[
        str, typer.Argument(help="Custom semantic type ID to hide from new schemas.")
    ],
    workspace_path: Annotated[
        Path | None, typer.Option("--workspace", help=WORKSPACE_HELP)
    ] = None,
) -> None:
    """Hide a custom type from new assignments; existing datasets remain searchable."""
    if type_id.upper() in BUILTIN_TYPE_IDS:
        raise PandoracleError("built-in semantic types cannot be retired")
    workspace = Workspace.open(_workspace_path(workspace_path))
    with workspace.lock(exclusive=True):
        changed = workspace.catalog.set_custom_type_retired(type_id, retired=True)
    console.print(
        f"Custom type {type_id!r} "
        + ("retired; existing datasets remain searchable." if changed else "is already retired.")
    )


@types_app.command(
    "restore", epilog="Example: pandoracle types restore customer_code"
)
def types_restore_command(
    type_id: Annotated[str, typer.Argument(help="Retired custom semantic type ID to enable.")],
    workspace_path: Annotated[
        Path | None, typer.Option("--workspace", help=WORKSPACE_HELP)
    ] = None,
) -> None:
    """Make a retired custom type available to new schema assignments again."""
    if type_id.upper() in BUILTIN_TYPE_IDS:
        raise PandoracleError("built-in semantic types are always active")
    workspace = Workspace.open(_workspace_path(workspace_path))
    with workspace.lock(exclusive=True):
        changed = workspace.catalog.set_custom_type_retired(type_id, retired=False)
    console.print(
        f"Custom type {type_id!r} "
        + ("restored." if changed else "is already active.")
    )


@schema_app.command(
    "revise",
    epilog=(
        "The previous active version remains available by RecordRef. Example: "
        "pandoracle schema revise people --schema people.schema.json"
    ),
)
def schema_revise_command(
    dataset: Annotated[str, typer.Argument(help="Logical dataset name or dataset UUID.")],
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    schema: Annotated[
        Path | None,
        typer.Option("--schema", help="Fully confirmed schema plan JSON."),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Publish and activate a corrected, fully confirmed dataset schema."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    draft = analyze_dataset(workspace, dataset)
    interactive = output_format is OutputFormat.TABLE and sys.stdin.isatty()
    plan = _resolve_cli_schema(
        draft,
        schema,
        interactive=interactive,
        existing_custom=workspace.catalog.list_custom_types(include_retired=False),
        workspace=workspace,
    )
    if output_format is OutputFormat.JSON:
        result = revise_dataset(workspace, dataset, plan)
    else:
        with ImportProgressRenderer(error_console) as progress_display:
            result = revise_dataset(
                workspace,
                dataset,
                plan,
                progress=progress_display.update,
            )
    if output_format is OutputFormat.JSON:
        _emit_json(result.to_dict())
    else:
        action = "Reused" if result.deduplicated else "Published"
        console.print(f"{action} schema revision for {result.dataset_name!r}")
        console.print(f"Version: {result.dataset_version_id}")
        console.print(f"Rows: {result.row_count:,}; postings: {result.posting_count:,}")


def _prompt_for_workspace() -> Workspace | None:
    prompts = QuestionaryPromptBackend()
    action = prompts.select(
        "Workspace setup",
        [
            PromptChoice("Create the standard workspace", "create", ChoiceKind.RECOMMENDED),
            PromptChoice("Select an existing workspace", "select", ChoiceKind.UTILITY),
            PromptChoice("Quit", "quit", ChoiceKind.DESTRUCTIVE),
        ],
        default="create",
    )
    if action in {None, "quit"}:
        return None
    if action == "create":
        standard = default_workspace_path()
        workspace = Workspace.open(standard) if standard.exists() else Workspace.create(standard)
    else:
        value = prompts.text("Existing workspace path")
        if value is None or not value.strip():
            return None
        workspace = Workspace.open(Path(value.strip()))
    select_workspace(workspace.root)
    return workspace


@app.callback(invoke_without_command=True)
def root_command(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version", help="Print the Pandoracle version and exit.", is_eager=True
        ),
    ] = False,
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
) -> None:
    """Open private interactive search when no subcommand is supplied."""
    if version:
        console.print(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    if not _interactive_terminal():
        console.print(ctx.get_help())
        return
    _emit_brand()
    configured = workspace_path or (
        Path(value) if (value := os.environ.get("PANDORACLE_WORKSPACE")) else None
    )
    path = configured if configured is not None else selected_workspace()
    workspace = Workspace.open(path) if path is not None else _prompt_for_workspace()
    if workspace is None:
        return
    if not workspace.catalog.list_sources():
        console.print(f"Workspace ready: {workspace.root}")
        console.print("Next: pandoracle import /path/to/data.csv")
        return
    _run_shell(workspace, show_brand=False)


@app.command(epilog="Example: pandoracle init")
def init(
    path: Annotated[
        Path | None,
        typer.Argument(help="New workspace path. Defaults to the standard XDG data location."),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Create and select an empty workspace, ready for import."""
    workspace = Workspace.create(path or default_workspace_path())
    select_workspace(workspace.root)
    result = {
        "workspace_id": workspace.manifest.workspace_id,
        "workspace": str(workspace.root),
        "format": workspace.manifest.format.__dict__,
        "selected": True,
    }
    if output_format is OutputFormat.JSON:
        _emit_json(result)
    else:
        console.print(f"Workspace initialized: {workspace.root}")
        console.print(f"Workspace ID: {workspace.manifest.workspace_id}")
        console.print("Next: pandoracle import /path/to/data.csv")


@app.command("workspace", epilog="Example: pandoracle workspace /data/pandoracle")
def workspace_command(
    path: Annotated[
        Path | None,
        typer.Argument(help="Existing workspace to select. Omit to show the current selection."),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Show or select the workspace used by ordinary commands."""
    if path is None:
        current = selected_workspace()
        if current is None:
            raise PandoracleError("no workspace is selected; run 'pandoracle init'")
        workspace = Workspace.open(current)
    else:
        workspace = Workspace.open(path)
        select_workspace(workspace.root)
    value = {"workspace": str(workspace.root), "workspace_id": workspace.manifest.workspace_id}
    if output_format is OutputFormat.JSON:
        _emit_json(value)
    else:
        console.print(f"Selected workspace: {workspace.root}")


@app.command(
    "import",
    epilog=(
        "Interactive terminals can review inferred types in place. Automation must pass a "
        "confirmed plan with --schema. Example: pandoracle import contacts.csv "
        "--dataset contacts"
    ),
)
def import_command(
    source: Annotated[Path, typer.Argument(help="CSV source file outside the workspace.")],
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    dataset: Annotated[
        str | None, typer.Option("--dataset", help="Stable logical dataset name")
    ] = None,
    encoding: Annotated[
        str,
        typer.Option("--encoding", help="Character encoding used to decode the CSV source."),
    ] = "utf-8",
    schema: Annotated[
        Path | None,
        typer.Option("--schema", help="Fully confirmed schema plan JSON."),
    ] = None,
    on_error: Annotated[
        MalformedRecordPolicy | None,
        typer.Option(
            "--on-error",
            help=(
                "Malformed-record policy for automation: abort or quarantine. Without this "
                "option, non-interactive imports abort strictly and interactive imports prompt."
            ),
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Import a CSV after confirming what each column means."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    draft = analyze_csv(source, encoding=encoding)
    interactive = output_format is OutputFormat.TABLE and sys.stdin.isatty()
    plan = _resolve_cli_schema(
        draft,
        schema,
        interactive=interactive,
        existing_custom=workspace.catalog.list_custom_types(include_retired=False),
        workspace=workspace,
    )
    if output_format is OutputFormat.JSON:
        result = import_csv(
            workspace,
            source,
            dataset_name=dataset,
            encoding=encoding,
            schema_plan=plan,
            on_error=on_error,
        )
    else:
        with ImportProgressRenderer(error_console) as progress_display:
            prompts = QuestionaryPromptBackend()
            malformed_handler = (
                None
                if on_error is not None or not interactive
                else lambda record: _prompt_malformed_record(record, prompts, progress_display)
            )
            escalation_handler = (
                (lambda value: _prompt_reject_escalation(value, prompts, progress_display))
                if interactive
                and (
                    malformed_handler is not None
                    or on_error is MalformedRecordPolicy.QUARANTINE
                )
                else None
            )
            result = import_csv(
                workspace,
                source,
                dataset_name=dataset,
                encoding=encoding,
                schema_plan=plan,
                progress=progress_display.update,
                on_error=on_error,
                malformed_handler=malformed_handler,
                escalation_handler=escalation_handler,
            )
    value = result.to_dict()
    if output_format is OutputFormat.JSON:
        _emit_json(value)
    else:
        action = "Reused" if result.deduplicated else "Published"
        console.print(f"{action} dataset {result.dataset_name!r}")
        console.print(f"Accepted records: {result.row_count:,}")
        console.print(f"Quarantined records: {result.quarantined_count:,}")
        if result.quarantine_path is not None:
            console.print(f"Quarantine: {result.quarantine_path}")
        console.print(f"Version: {result.dataset_version_id}")
        console.print(f"RAW SHA-256: {result.source_sha256}")
        typed = [
            f"{field['source_name']} → {field['semantic_type']}"
            for field in result.fields
            if field["semantic_type"] != "UNKNOWN"
        ]
        console.print("Semantic fields: " + (", ".join(typed) if typed else "none"))
        console.print("Searchable now: projected scan (acceleration is optional)")
        if interactive and not result.deduplicated:
            proposal = make_acceleration_plan(workspace, result.dataset_id)
            choice = QuestionaryPromptBackend().select(
                "Acceleration",
                [
                    PromptChoice("Keep scan-only for now", "scan", ChoiceKind.RECOMMENDED),
                    PromptChoice(
                        "Review and build acceleration", "build", ChoiceKind.UTILITY
                    ),
                ],
                default="scan",
            )
            if choice == "build":
                try:
                    confirmed = _review_acceleration_plan(proposal)
                    with ImportProgressRenderer(error_console) as display:
                        acceleration = configure_acceleration(
                            workspace,
                            result.dataset_id,
                            confirmed,
                            progress=display.update,
                        )
                    console.print(f"Published acceleration profile {acceleration.index_profile_id}")
                except ImportFailure as error:
                    error_console.print(
                        f"Acceleration not changed; dataset remains searchable: {error}"
                    )


@app.command("datasets", epilog="Example: pandoracle datasets")
def datasets_command(
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """List imported datasets, searchable fields, and acceleration status."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    value = workspace.catalog.list_sources()
    if output_format is OutputFormat.JSON:
        _emit_json(value)
    else:
        _emit_datasets(value)


@app.command(
    epilog=(
        "Use the <dataset-version-id>:<ordinal> RecordRef from search to print a complete "
        "record with provenance. Example: pandoracle inspect VERSION_UUID:42"
    ),
)
def inspect(
    identifier: Annotated[
        str, typer.Argument(help="Dataset name/UUID or <dataset-version-id>:<ordinal>")
    ],
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Inspect dataset metadata or one source record."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    if ":" in identifier:
        value = inspect_record(
            workspace, identifier, include_fields=output_format is OutputFormat.TABLE
        )
    else:
        value = workspace.catalog.find_dataset(identifier)
        if value is None:
            raise PandoracleError(f"dataset not found: {identifier}")
    if output_format is OutputFormat.JSON:
        _emit_json(value)
    elif ":" in identifier:
        _emit_inspected_record(value)
    else:
        console.print_json(data=value)


@app.command(
    "search",
    epilog=(
        "Without --dataset, all active datasets are searched. Table output prints compact "
        "hits and RecordRefs; JSON includes complete records and provenance. Example: "
        "pandoracle search --type email --format json person@example.test. Compound example: "
        "pandoracle search --clue PERSON_NAME:token=Vadim --clue "
        "DATE_OF_BIRTH:year=2008 --scan"
    ),
)
def search_command(
    value: Annotated[
        str | None,
        typer.Argument(help="Value to interpret using the semantic type's AUTO operator."),
    ] = None,
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    semantic_type: Annotated[
        str | None,
        typer.Option(
            "--type",
            help="Force one built-in or workspace custom semantic type instead of inference.",
        ),
    ] = None,
    dataset: Annotated[
        str | None,
        typer.Option("--dataset", help="Restrict search to one logical dataset name or UUID."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=10_000, help="Maximum number of hits to return."),
    ] = 50,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
    explain: Annotated[
        bool,
        typer.Option(
            "--explain",
            help="Include the chosen search paths and timings.",
        ),
    ] = False,
    clues: Annotated[
        list[str] | None,
        typer.Option(
            "--clue",
            help="Repeat TYPE[:OPERATOR]=VALUE; operators are auto, exact, token, range, year.",
        ),
    ] = None,
    allow_scan: Annotated[
        bool,
        typer.Option(
            "--scan",
            help="Allow an expensive chosen scan; this never forces scanning.",
        ),
    ] = False,
) -> None:
    """Search active datasets from semantic clues; planning is automatic."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    if clues:
        if value is not None or semantic_type is not None:
            raise PandoracleError("do not mix positional VALUE/--type with --clue")
        parsed = tuple(_parse_query_clue(item) for item in clues)
    else:
        if value is None:
            raise PandoracleError("provide VALUE or at least one --clue")
        if semantic_type is None:
            inferred = classify_query(value)
            if not inferred:
                raise SearchFailure("could not infer a searchable semantic type; pass --type")
            identifier = inferred[0][0].value
        else:
            identifier = (
                semantic_type.upper()
                if semantic_type.upper() in BUILTIN_TYPE_IDS
                else semantic_type
            )
        parsed = (SearchClue(identifier, value, SearchOperator.AUTO),)
    request = SearchRequest(
        parsed,
        dataset=dataset,
        limit=limit,
        allow_expensive_scan=allow_scan,
    )
    if output_format is OutputFormat.TABLE:
        with ImportProgressRenderer(error_console) as display:
            result = execute_search(workspace, request, progress=display.update)
    else:
        result = execute_search(workspace, request)
    if output_format is OutputFormat.JSON:
        _emit_json(result.to_dict(explain=explain))
    else:
        _emit_search_result(result, explain=explain)


@app.command(
    epilog=(
        "Bare input is a quick search: Pandoracle infers one semantic type and searches "
        "matching fields in every active dataset. Use :search for explicit control or "
        ":help inside the shell. Example: pandoracle shell"
    )
)
def shell(
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
) -> None:
    """Search and refine interactively without saving entered queries."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    _run_shell(workspace, show_brand=True)


def _run_shell(workspace: Workspace, *, show_brand: bool) -> None:
    if show_brand and _interactive_terminal():
        _emit_brand()
    prompts = QuestionaryPromptBackend()
    console.print("Private search shell. Type :help for help.")
    while True:
        try:
            value = typer.prompt("search", prompt_suffix=" > ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if value in {":quit", ":exit"}:
            return
        if value == ":datasets":
            _emit_datasets(workspace.catalog.list_sources())
            continue
        if value == ":types":
            _emit_semantic_types(workspace)
            continue
        if value == ":help":
            _emit_shell_help()
            continue
        if value == ":search":
            try:
                query = _prompt_candidate_query(workspace, prompts)
                if query is None:
                    continue
                result = _run_shell_candidate_query(workspace, query, prompts)
                if result is None:
                    continue
                candidates, execution = result
                _candidate_shell_session(workspace, query, candidates, execution, prompts)
            except (EOFError, KeyboardInterrupt):
                console.print()
                return
            except PandoracleError as error:
                error_console.print(f"Error: {error}")
            continue
        try:
            inferred = classify_query(value)
            if not inferred:
                raise SearchFailure(
                    "could not infer a searchable semantic type; use :search to choose one"
                )
            _emit_shell_auto_plan(inferred[0][0])
            result = search(
                workspace,
                SearchRequest((SearchClue(inferred[0][0], value),)),
            )
            _emit_search_result(result, explain=False)
        except PandoracleError as error:
            error_console.print(f"Error: {error}")


@maintenance_app.command(
    "recover",
    epilog=(
        "Run only after confirming no writer process is active. Staging data is moved to "
        "operations/orphans, never silently deleted. Example: pandoracle maintenance recover "
        "--format json"
    ),
)
def recover_command(
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Abort interrupted operations and quarantine their staging data."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    value = recover(workspace)
    if output_format is OutputFormat.JSON:
        _emit_json(value)
    else:
        console.print(f"Recovered operations: {len(value)}")
        for item in value:
            console.print(f"- {item['operation_id']} → {item['quarantined_path']}")


@maintenance_app.command(
    "gc",
    epilog=(
        "The default is a non-mutating dry run. Run recover first if an interrupted "
        "operation is reported. Example: pandoracle maintenance gc --apply"
    ),
)
def gc_command(
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Reclaim the freshly re-planned unreferenced state instead of a dry run.",
        ),
    ] = False,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Discover or reclaim abandoned workspace storage with reference checks."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    value = apply_gc(workspace) if apply else plan_gc(workspace)
    if output_format is OutputFormat.JSON:
        _emit_json(value.to_dict())
    else:
        _emit_gc(value)


@maintenance_app.command(epilog="Example: pandoracle maintenance operations")
def operations(
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
) -> None:
    """Show the durable operation journal."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    _emit_json(list_operations(workspace))


@maintenance_app.command(
    epilog=(
        "Verification hashes every registered RAW and artifact file and may take a long "
        "time. Example: pandoracle maintenance verify --format json"
    ),
)
def verify(
    workspace_path: Annotated[
        Path | None,
        typer.Option("--workspace", envvar="PANDORACLE_WORKSPACE", help=WORKSPACE_HELP),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """Verify source and search artifacts against the catalog."""
    workspace = Workspace.open(_workspace_path(workspace_path))
    value = verify_workspace(workspace)
    if output_format is OutputFormat.JSON:
        _emit_json(value)
    else:
        console.print(
            f"Workspace OK: {value['raw_blobs']} RAW blobs, "
            f"{value['artifacts']} artifacts, {value['bytes_verified']:,} bytes"
        )


def _device_service() -> DeviceService:
    return DeviceService()


def _device_policy(
    selector: str | None, prompts: PromptBackend | None = None
) -> HostDevicePolicy:
    policies = list(load_host_devices().devices.values())
    if selector:
        parsed = urlparse(selector)
        value = parsed.netloc or parsed.path.lstrip("/") if parsed.scheme else selector
        matches = [
            item
            for item in policies
            if value in {item.device_id, item.name, item.public_uuid, item.luks_uuid}
        ]
        if len(matches) == 1:
            return matches[0]
        raise DeviceError(f"trusted Pandora device not found: {value}")
    if len(policies) == 1:
        return policies[0]
    if not policies:
        raise DeviceError("no trusted Pandora devices; run 'pandoracle device setup' or trust")
    if not sys.stdin.isatty():
        raise DeviceError("select a device by name or ID")
    prompts = prompts or QuestionaryPromptBackend()
    selected = prompts.select(
        "Pandora device",
        [
            PromptChoice(f"{policy.name} ({policy.device_id[:8]})", policy.device_id)
            for policy in policies
        ]
        + [PromptChoice("Cancel", "__cancel__", ChoiceKind.DESTRUCTIVE)],
    )
    if selected in {None, "__cancel__"}:
        raise DeviceError("device selection cancelled")
    return next(policy for policy in policies if policy.device_id == selected)


def _drive_candidate(
    candidates: list[DriveCandidate], prompts: PromptBackend | None = None
) -> DriveCandidate:
    if not candidates:
        raise DeviceError("no unused removable whole drive is available")
    prompts = prompts or QuestionaryPromptBackend()
    selected = prompts.select(
        "Drive to erase",
        [
            PromptChoice(candidate.display_name, candidate.object_path)
            for candidate in candidates
        ]
        + [PromptChoice("Cancel", "__cancel__", ChoiceKind.DESTRUCTIVE)],
    )
    if selected in {None, "__cancel__"}:
        raise DeviceError("device setup cancelled")
    return next(candidate for candidate in candidates if candidate.object_path == selected)


def _untrusted_device(
    devices: list[ConnectedDevice], prompts: PromptBackend | None = None
) -> ConnectedDevice:
    trusted_luks = {item.luks_uuid for item in load_host_devices().devices.values()}
    choices = [item for item in devices if item.luks_uuid not in trusted_luks]
    if not choices:
        raise DeviceError("no untrusted Pandora device is connected")
    if len(choices) == 1:
        return choices[0]
    prompts = prompts or QuestionaryPromptBackend()
    selected = prompts.select(
        "Pandora device",
        [
            PromptChoice(f"Pandora drive {item.public_uuid[:8]}", item.luks_uuid)
            for item in choices
        ]
        + [PromptChoice("Cancel", "__cancel__", ChoiceKind.DESTRUCTIVE)],
    )
    if selected in {None, "__cancel__"}:
        raise DeviceError("device selection cancelled")
    return next(item for item in choices if item.luks_uuid == selected)


def _credential_validation(value: str) -> bool | str:
    if not value:
        return "Credential must not be empty."
    if "\x00" in value:
        return "Credential must not contain a NUL character."
    return True


def _prompt_credential(
    label: str, prompts: PromptBackend | None = None
) -> bytearray:
    prompts = prompts or QuestionaryPromptBackend()
    value = prompts.password(label, validate=_credential_validation)
    if value is None:
        raise DeviceError("credential entry cancelled")
    validation = _credential_validation(value)
    if validation is not True:
        raise DeviceError(str(validation))
    return bytearray(value, "utf-8")


def _public_directory_validation(value: str) -> bool | str:
    if not value.strip():
        return True
    try:
        inspect_public_directory(Path(value.strip()))
    except DeviceError as error:
        return str(error)
    return True


def _emit_public_warning(directory: Path | None, files: int = 0) -> None:
    warning = Text("PUBLIC AND UNENCRYPTED: ", style="bold red")
    if directory is None:
        warning.append("PANDORA_PUB will start with no user files.")
    else:
        warning.append(
            f"all {files:,} files under {directory} will be visible on PANDORA_PUB."
        )
    console.print(warning)


@device_app.command("setup", epilog="Example: pandoracle device setup")
def device_setup_command(
    name: Annotated[
        str | None, typer.Option("--name", help="Friendly name stored only on this host.")
    ] = None,
    automatic_unlock: Annotated[
        bool,
        typer.Option(
            "--automatic-unlock",
            help=(
                "Explicitly let this host unlock the drive using its own revocable key "
                "stored in Secret Service. Anyone controlling this login can then unlock it."
            ),
        ),
    ] = False,
    public_directory: Annotated[
        Path | None,
        typer.Option(
            "--public-directory",
            help=(
                "Copy this local directory hierarchy to the public, unencrypted partition."
            ),
        ),
    ] = None,
) -> None:
    """Erase a removable drive and create public and encrypted private areas."""
    if not sys.stdin.isatty():
        raise DeviceError("device setup requires an interactive terminal")
    prompts = QuestionaryPromptBackend()
    service = _device_service()
    candidate = _drive_candidate(service.candidates(), prompts)
    if public_directory is None:
        value = prompts.text(
            "Public content directory (leave empty for none)",
            validate=_public_directory_validation,
        )
        if value is None:
            raise DeviceError("device setup cancelled")
        public_directory = Path(value.strip()).expanduser() if value.strip() else None
    if public_directory is not None:
        public_directory = public_directory.expanduser()
        summary = inspect_public_directory(public_directory)
        _emit_public_warning(public_directory, summary.files)
    else:
        _emit_public_warning(None)
    phrase = "ERASE THIS DRIVE"
    console.print(
        f"This permanently erases the whole {candidate.display_name} drive. "
        f"Type {phrase!r} to continue."
    )
    confirmation = prompts.text("Confirmation")
    if confirmation != phrase:
        raise DeviceError("device setup cancelled")
    first = _prompt_credential("Choose main passphrase", prompts)
    try:
        second = _prompt_credential("Repeat main passphrase", prompts)
    except BaseException:
        for index in range(len(first)):
            first[index] = 0
        raise
    if first != second:
        for index in range(len(first)):
            first[index] = 0
        for index in range(len(second)):
            second[index] = 0
        raise DeviceError("passphrases do not match")
    for index in range(len(second)):
        second[index] = 0

    def acknowledge(recovery: str) -> bool:
        console.print("\nRecovery credential (shown once):", style="bold")
        console.print(recovery, style="bold yellow")
        console.print(
            "Store this separately in a password manager or offline. Pandoracle cannot "
            "recover or display it later."
        )
        action = prompts.select(
            "Recovery credential",
            [
                PromptChoice("I have stored it safely", "stored", ChoiceKind.UTILITY),
                PromptChoice("Cancel setup", "cancel", ChoiceKind.DESTRUCTIVE),
            ],
            default="cancel",
        )
        return action == "stored"

    console.print(
        "One administrator authorization will provision the complete removable drive."
    )
    result = service.setup(
        candidate,
        name=name or "Pandora",
        main_passphrase=first,
        acknowledge_recovery=acknowledge,
        automatic_unlock=automatic_unlock,
        public_directory=public_directory,
    )
    console.print(f"Pandora device ready: {result.name} ({result.device_id[:8]})")
    if result.automatic_unlock_error is not None:
        error_console.print(
            "Automatic unlock was not enabled; prompt unlock remains available: "
            f"{result.automatic_unlock_error}"
        )
    try:
        enable_detection()
    except DeviceError as error:
        error_console.print(
            "Device detection was not enabled; prompt-based use is ready. Run "
            f"'pandoracle device detection enable' later: {error}"
        )


@device_app.command("trust", epilog="Example: pandoracle device trust --name Field kit")
def device_trust_command(
    name: Annotated[
        str | None, typer.Option("--name", help="Friendly host-only device name.")
    ] = None,
    automatic_unlock: Annotated[
        bool,
        typer.Option(
            "--automatic-unlock",
            help="Enroll a separate random host key and keep it in Secret Service.",
        ),
    ] = False,
) -> None:
    """Trust an existing Pandora drive on this Linux host."""
    service = _device_service()
    connected = _untrusted_device(service.probable_devices())
    credential = _prompt_credential("Main or recovery credential")
    policy = service.trust(
        connected,
        name=name or "Pandora",
        credential=credential,
        automatic_unlock=automatic_unlock,
    )
    console.print(f"Trusted {policy.name} on this host.")
    try:
        enable_detection()
    except DeviceError as error:
        error_console.print(
            "Device detection was not enabled; manual opening is ready. Run "
            f"'pandoracle device detection enable' later: {error}"
        )


@device_app.command("list", epilog="Example: pandoracle device list")
def device_list_command(
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help=FORMAT_HELP)
    ] = OutputFormat.TABLE,
) -> None:
    """List this host's trusted Pandora devices and current presence."""
    service = _device_service()
    policies = list(load_host_devices().devices.values())
    rows = [
        {
            **policy.to_dict(),
            "connected": service.udisks.connected_for_policy(policy) is not None,
        }
        for policy in policies
    ]
    if output_format is OutputFormat.JSON:
        _emit_json(rows)
        return
    table = Table(title="Pandora devices")
    table.add_column("Name")
    table.add_column("Connected")
    table.add_column("Auto-open")
    table.add_column("Unlock")
    table.add_column("ID")
    for row in rows:
        table.add_row(
            str(row["name"]),
            "yes" if row["connected"] else "no",
            "yes" if row["auto_open"] else "no",
            str(row["unlock_mode"]),
            str(row["device_id"])[:8],
        )
    console.print(table)


@device_app.command("open", epilog="Example: pandoracle device open 'My Pandora'")
def device_open_command(
    device: Annotated[
        str | None,
        typer.Argument(help="Friendly name, device ID, or desktop-launch URI."),
    ] = None,
    auto: Annotated[
        bool, typer.Option("--auto", hidden=True, help="Opened by the desktop watcher.")
    ] = False,
) -> None:
    """Unlock, recover, and supervise the private shell until the drive closes."""
    del auto
    policy = _device_policy(device)

    def retry_close(error: str) -> None:
        error_console.print(f"Could not safely close the device: {error}")
        error_console.print(
            "Waiting and retrying without force. Finish commands or close file-manager "
            "windows using the device; physical removal will end this terminal."
        )

    _device_service().open_session(
        policy,
        _prompt_credential,
        close_retry=retry_close,
    )


@device_app.command("close", epilog="Example: pandoracle device close")
def device_close_command(
    device: Annotated[str | None, typer.Argument(help="Friendly name or device ID.")] = None,
) -> None:
    """Flush, unmount, lock, and safely power off a connected Pandora drive."""
    policy = _device_policy(device)
    _device_service().close(policy)
    console.print(f"Closed {policy.name} safely.")


@device_app.command("forget", epilog="Example: pandoracle device forget 'My Pandora'")
def device_forget_command(
    device: Annotated[str | None, typer.Argument(help="Friendly name or device ID.")] = None,
) -> None:
    """Remove this host's policy and auto-unlock key without changing owner credentials."""
    policy = _device_policy(device)
    action = QuestionaryPromptBackend().select(
        f"Forget {policy.name} on this host?",
        [
            PromptChoice("Keep device", "keep", ChoiceKind.RECOMMENDED),
            PromptChoice("Forget device", "forget", ChoiceKind.DESTRUCTIVE),
        ],
        default="keep",
    )
    if action != "forget":
        raise DeviceError("device forget cancelled")
    result = _device_service().forget(policy)
    console.print(f"Forgot {result.name} on this host.")
    if result.orphaned_key_possible:
        console.print(
            "The drive was absent, so an unusable orphan keyslot may remain on it. "
            "The deleted host secret cannot be recovered or used."
        )


@device_app.command("settings", epilog="Example: pandoracle device settings --auto-open")
def device_settings_command(
    device: Annotated[str | None, typer.Argument(help="Friendly name or device ID.")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Change the host-only name.")] = None,
    auto_open: Annotated[
        bool | None,
        typer.Option("--auto-open/--no-auto-open", help="Open a terminal when inserted."),
    ] = None,
    unlock: Annotated[
        UnlockMode | None,
        typer.Option("--unlock", help="Use prompt or explicit per-host automatic unlock."),
    ] = None,
) -> None:
    """Show or change per-host opening and unlock behavior."""
    service = _device_service()
    policy = _device_policy(device)
    if unlock is UnlockMode.AUTOMATIC and policy.unlock_mode is UnlockMode.PROMPT:
        console.print(
            "Automatic unlock stores a separate device key in this login's Secret Service. "
            "Anyone controlling the unlocked login can use that key to unlock this drive."
        )
        credential = _prompt_credential("Main or recovery credential")
        policy = service.enable_automatic(policy, credential)
    elif unlock is UnlockMode.PROMPT and policy.unlock_mode is UnlockMode.AUTOMATIC:
        policy = service.disable_automatic(policy)
    if name is not None or auto_open is not None:
        policy = replace(
            policy,
            name=(name.strip() or policy.name) if name is not None else policy.name,
            auto_open=auto_open if auto_open is not None else policy.auto_open,
        )
        service.update_policy(policy)
    console.print(f"Name: {policy.name}")
    console.print(f"Automatic opening: {'enabled' if policy.auto_open else 'disabled'}")
    console.print(f"Unlock: {policy.unlock_mode.value}")


@device_app.command(
    "public", epilog="Example: pandoracle device public ~/pandora-public"
)
def device_public_command(
    directory: Annotated[
        Path, typer.Argument(help="Local directory to copy to PANDORA_PUB.")
    ],
    device: Annotated[
        str | None, typer.Option("--device", help="Friendly name or device ID.")
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Replace existing public contents without prompting."),
    ] = False,
) -> None:
    """Replace all user-visible public files from a local directory."""
    directory = directory.expanduser()
    preview = inspect_public_directory(directory)
    policy = _device_policy(device)
    _emit_public_warning(directory, preview.files)
    console.print(f"This replaces the current public files on {policy.name}.")
    if not yes:
        if not sys.stdin.isatty():
            raise DeviceError("non-interactive public replacement requires --yes")
        action = QuestionaryPromptBackend().select(
            "Public contents",
            [
                PromptChoice("Keep current contents", "keep", ChoiceKind.RECOMMENDED),
                PromptChoice("Replace public contents", "replace", ChoiceKind.DESTRUCTIVE),
            ],
            default="keep",
        )
        if action != "replace":
            raise DeviceError("public content replacement cancelled")
    result = _device_service().replace_public_contents(policy, directory)
    console.print(
        f"Replaced public contents on {policy.name}: {result.files:,} files, "
        f"{result.directories:,} directories, {result.bytes:,} bytes."
    )


@detection_app.callback(invoke_without_command=True)
def device_detection_command(ctx: typer.Context) -> None:
    """Show whether insertion detection is enabled for this login."""
    if ctx.invoked_subcommand is None:
        console.print(f"Device detection: {'enabled' if detection_enabled() else 'disabled'}")


@detection_app.command("enable", epilog="Example: pandoracle device detection enable")
def device_detection_enable_command() -> None:
    """Install and start the per-user desktop insertion watcher."""
    enable_detection()
    console.print("Pandoracle device detection enabled.")


@detection_app.command("disable", epilog="Example: pandoracle device detection disable")
def device_detection_disable_command() -> None:
    """Disable automatic detection without forgetting any device."""
    disable_detection()
    console.print("Pandoracle device detection disabled.")


@device_app.command("watch", hidden=True)
def device_watch_command() -> None:
    """Run the per-user device event watcher."""
    run_watcher()

def main() -> None:
    try:
        app()
    except PandoracleError as error:
        error_console.print(f"Error: {error}")
        # This handler runs outside Click/Typer's command lifecycle. Raising
        # typer.Exit here leaks a traceback; SystemExit is the correct boundary.
        raise SystemExit(2) from None
    except Exception:
        if os.environ.get("PANDORACLE_DEBUG") == "1":
            raise
        error_console.print(
            "Error: unexpected internal failure (set PANDORACLE_DEBUG=1 to show the traceback)"
        )
        raise SystemExit(1) from None
