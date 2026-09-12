from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from rich.columns import Columns
from rich.console import Console, RenderableType
from rich.filesize import decimal
from rich.progress import (
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.progress_bar import ProgressBar
from rich.text import Text

from pandoracle.errors import ImportFailure
from pandoracle.normalize import BUILTIN_TYPE_IDS
from pandoracle.schema import CustomTypeSpec, SchemaPlan, confirm_plan


class ChoiceKind(StrEnum):
    DATA = "data"
    RECOMMENDED = "recommended"
    UTILITY = "utility"
    NAVIGATION = "navigation"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class PromptChoice:
    title: str
    value: str
    kind: ChoiceKind = ChoiceKind.DATA


class PromptBackend(Protocol):
    def select(
        self,
        message: str,
        choices: Sequence[PromptChoice],
        *,
        default: str | None = None,
    ) -> str | None: ...

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Callable[[str], bool | str] | None = None,
    ) -> str | None: ...

    def password(
        self,
        message: str,
        *,
        validate: Callable[[str], bool | str] | None = None,
    ) -> str | None: ...


class QuestionaryPromptBackend:
    """Load Questionary only when an interactive prompt is actually shown."""

    @staticmethod
    def _module() -> Any:
        import questionary

        return questionary

    @staticmethod
    def _style(questionary: Any) -> Any:
        return questionary.Style(
            [
                ("choice.recommended", "fg:ansigreen bold"),
                ("choice.utility", "fg:ansicyan"),
                ("choice.navigation", "fg:ansicyan"),
                ("choice.destructive", "fg:ansired bold"),
                ("separator", "fg:ansibrightblack"),
            ]
        )

    def select(
        self,
        message: str,
        choices: Sequence[PromptChoice],
        *,
        default: str | None = None,
    ) -> str | None:
        questionary = self._module()
        labels = {
            ChoiceKind.DATA: "Data choices",
            ChoiceKind.RECOMMENDED: "Recommended",
            ChoiceKind.UTILITY: "Actions",
            ChoiceKind.NAVIGATION: "Navigation",
            ChoiceKind.DESTRUCTIVE: "Destructive",
        }
        values = []
        previous_kind: ChoiceKind | None = None
        show_groups = len(choices) >= 5 and len({item.kind for item in choices}) > 1
        for item in choices:
            if show_groups and item.kind is not previous_kind:
                values.append(questionary.Separator(f"── {labels[item.kind]} ──"))
            values.append(
                questionary.Choice(
                    [(f"class:choice.{item.kind.value}", item.title)],
                    value=item.value,
                )
            )
            previous_kind = item.kind
        use_search_filter = len(choices) > 15
        instruction = (
            "(type to filter; use arrows; Enter selects; Ctrl-C cancels)"
            if use_search_filter
            else "(use arrows or j/k; Enter selects; Ctrl-C cancels)"
        )
        return questionary.select(
            message,
            choices=values,
            default=default,
            instruction=instruction,
            use_arrow_keys=True,
            use_jk_keys=not use_search_filter,
            use_emacs_keys=True,
            style=self._style(questionary),
            use_search_filter=use_search_filter,
        ).ask(kbi_msg="")

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Callable[[str], bool | str] | None = None,
    ) -> str | None:
        questionary = self._module()
        return questionary.text(
            message,
            default=default,
            validate=validate,
            style=self._style(questionary),
        ).ask(kbi_msg="")

    def password(
        self,
        message: str,
        *,
        validate: Callable[[str], bool | str] | None = None,
    ) -> str | None:
        questionary = self._module()
        return questionary.password(
            message,
            validate=validate,
            style=self._style(questionary),
        ).ask(kbi_msg="")


_TYPE_DESCRIPTIONS = {
    "EMAIL": "normalized email address",
    "PHONE": "normalized telephone number",
    "DOMAIN": "normalized DNS domain",
    "URL": "normalized HTTP(S) URL",
    "USERNAME": "normalized account name",
    "IP": "normalized IPv4 or IPv6 address",
    "DATE": "unambiguous calendar date",
    "DATE_OF_BIRTH": "unambiguous date of birth",
    "PERSON_NAME": "exact normalized person name",
    "UNKNOWN": "store only; do not index",
}

_TYPE_ORDER = (
    "UNKNOWN",
    "PERSON_NAME",
    "EMAIL",
    "PHONE",
    "DATE_OF_BIRTH",
    "DATE",
    "USERNAME",
    "DOMAIN",
    "URL",
    "IP",
)

_CONFIRM = "__confirm__"
_CANCEL = "__cancel__"
_BACK = "__back__"
_SAMPLES = "__samples__"
_NEW_TYPE = "__new_type__"


def emit_schema_plan(plan: SchemaPlan, console: Console, *, title: str = "Schema review") -> None:
    from rich.table import Table

    table = Table(title=title)
    table.add_column("#", justify="right")
    table.add_column("Column")
    table.add_column("Proposed")
    table.add_column("Score", justify="right")
    table.add_column("Selected")
    table.add_column("Canonicalizer")
    table.add_column("Evidence")
    for field in plan.fields:
        table.add_row(
            str(field.field_id),
            field.source_name,
            field.proposed_type,
            f"{field.score:.0%}",
            field.selected_type or "—",
            field.canonicalizer_id or "none",
            "; ".join(field.evidence) or "—",
        )
    table.caption = "Acceleration is configured separately after publication."
    console.print(table)


def _cancelled() -> ImportFailure:
    return ImportFailure("schema review cancelled")


def _custom_type_id_validator(
    value: str,
    existing_custom: dict[str, CustomTypeSpec],
    custom_types: dict[str, CustomTypeSpec],
    unavailable_type_ids: set[str],
) -> bool | str:
    identifier = value.strip()
    if (
        identifier in existing_custom
        or identifier in custom_types
        or identifier in unavailable_type_ids
    ):
        return "Type ID already exists; select it from the list"
    try:
        CustomTypeSpec(identifier, "Temporary label")
    except ValueError as error:
        return str(error)
    return True


def _custom_type_label_validator(identifier: str, value: str) -> bool | str:
    try:
        CustomTypeSpec(identifier, value.strip())
    except ValueError as error:
        return str(error)
    return True


def _type_choices(
    selected: str,
    existing_custom: dict[str, CustomTypeSpec],
    custom_types: dict[str, CustomTypeSpec],
) -> list[PromptChoice]:
    choices = [
        PromptChoice(
            f"{identifier} — {_TYPE_DESCRIPTIONS[identifier]}",
            identifier,
        )
        for identifier in _TYPE_ORDER
        if identifier in BUILTIN_TYPE_IDS and identifier != "UNKNOWN"
    ]
    for identifier, definition in sorted({**existing_custom, **custom_types}.items()):
        choices.append(
            PromptChoice(
                f"{identifier} — {definition.label} (custom exact text)",
                identifier,
            )
        )
    choices.extend(
        (
            PromptChoice(
                "Skip / leave UNKNOWN — store only; do not index",
                "UNKNOWN",
                ChoiceKind.UTILITY,
            ),
            PromptChoice("Create custom exact type…", _NEW_TYPE, ChoiceKind.UTILITY),
            PromptChoice("View source samples", _SAMPLES, ChoiceKind.UTILITY),
            PromptChoice("Back to schema summary", _BACK, ChoiceKind.NAVIGATION),
        )
    )
    if selected not in {choice.value for choice in choices}:
        choices.insert(0, PromptChoice(selected, selected))
    return choices


def review_schema_plan(
    plan: SchemaPlan,
    existing_custom: dict[str, CustomTypeSpec],
    *,
    console: Console,
    prompts: PromptBackend | None = None,
    unavailable_type_ids: set[str] | None = None,
) -> SchemaPlan:
    prompts = prompts or QuestionaryPromptBackend()
    unavailable_type_ids = unavailable_type_ids or set()
    selections = {
        field.field_id: field.selected_type or field.proposed_type for field in plan.fields
    }
    custom_types = {item.type_id: item for item in plan.custom_types}
    changed = False
    emit_schema_plan(
        confirm_plan(
            plan,
            selections,
            selection_source="user_confirmed",
            custom_types=tuple(custom_types.values()),
        ),
        console,
    )

    while True:
        menu = [PromptChoice("Confirm schema", _CONFIRM, ChoiceKind.RECOMMENDED)]
        menu.extend(
            PromptChoice(
                f"#{field.field_id} {field.source_name} → {selections[field.field_id]}",
                str(field.field_id),
            )
            for field in plan.fields
        )
        menu.append(PromptChoice("Cancel", _CANCEL, ChoiceKind.DESTRUCTIVE))
        action = prompts.select("Schema action", menu, default=_CONFIRM)
        if action is None or action == _CANCEL:
            raise _cancelled()
        if action == _CONFIRM:
            confirmed = confirm_plan(
                plan,
                selections,
                selection_source="user_confirmed",
                custom_types=tuple(custom_types.values()),
            )
            if changed:
                emit_schema_plan(confirmed, console, title="Confirmed schema")
            return confirmed

        try:
            field_id = int(action)
        except (TypeError, ValueError):
            raise ImportFailure("schema review returned an invalid column selection") from None
        field = next((item for item in plan.fields if item.field_id == field_id), None)
        if field is None:
            raise ImportFailure("schema review returned an unknown column selection")

        while True:
            selected = selections[field_id]
            type_action = prompts.select(
                f"Type for {field.source_name!r}",
                _type_choices(selected, existing_custom, custom_types),
                default=selected,
            )
            if type_action is None:
                raise _cancelled()
            if type_action == _BACK:
                break
            if type_action == _SAMPLES:
                if field.samples:
                    console.print(f"Samples for {field.source_name!r}:")
                    for sample in field.samples:
                        console.print(f"- {sample}")
                else:
                    console.print(
                        "Samples are not persisted; review directly from the source to see them."
                    )
                continue
            if type_action == _NEW_TYPE:
                identifier = prompts.text(
                    "Custom type ID (lowercase slug)",
                    validate=lambda value: _custom_type_id_validator(
                        value, existing_custom, custom_types, unavailable_type_ids
                    ),
                )
                if identifier is None:
                    raise _cancelled()
                identifier = identifier.strip()
                label = prompts.text(
                    "Display label",
                    default=field.source_name,
                    validate=lambda value, type_id=identifier: _custom_type_label_validator(
                        type_id, value
                    ),
                )
                if label is None:
                    raise _cancelled()
                default_operator = prompts.select(
                    "Default AUTO search for this type",
                    [
                        PromptChoice("Exact whole value", "EXACT"),
                        PromptChoice("All whitespace-delimited tokens", "TOKEN"),
                    ],
                    default="EXACT",
                )
                if default_operator is None:
                    raise _cancelled()
                definition = CustomTypeSpec(
                    identifier, label.strip(), default_operator=default_operator
                )
                custom_types[identifier] = definition
                selections[field_id] = identifier
                changed = True
                console.print(f"Selected {field.source_name!r} → {identifier}")
                selected = identifier
            else:
                if type_action not in BUILTIN_TYPE_IDS | set(existing_custom) | set(custom_types):
                    raise ImportFailure("schema review returned an unknown semantic type")
                selections[field_id] = type_action
                changed = changed or type_action != field.selected_type
                console.print(f"Selected {field.source_name!r} → {type_action}")
            changed = True
            break


class ProgressEvent(Protocol):
    @property
    def phase(self) -> Any: ...

    @property
    def completed(self) -> int: ...

    @property
    def total(self) -> int | None: ...

    @property
    def unit(self) -> str: ...


_PHASE_LABELS = {
    "COPYING_RAW": "Copying and hashing RAW",
    "ANALYZING": "Analyzing CSV schema",
    "TRANSFORMING": "Writing canonical and record Parquet",
    "INDEXING": "Building acceleration",
    "BUILDING": "Building acceleration",
    "FINALIZING": "Finalizing acceleration",
    "SCANNING": "Scanning canonical columns",
    "VALIDATING": "Validating artifacts",
    "PUBLISHING": "Publishing acceleration",
    "PUBLISHED": "Published",
}


def _quantity(value: float, unit: str) -> str:
    if unit == "bytes":
        return decimal(int(value))
    if unit:
        return f"{value:,.0f} {unit}"
    return f"{value:,.0f}"


def _duration(value: float) -> str:
    seconds = max(0, round(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


class _EventProgressColumn(ProgressColumn):
    def render(self, task: Task) -> RenderableType:
        event_total = task.fields.get("event_total")
        event_completed = float(task.fields.get("event_completed", task.completed))
        unit = str(task.fields.get("unit", ""))
        uses_overall = bool(task.fields.get("uses_overall", False))
        bar_total = task.total if uses_overall else event_total
        bar_completed = task.completed if uses_overall else event_completed
        bar = ProgressBar(total=bar_total, completed=bar_completed, width=24)
        if bar_total is None:
            detail = _quantity(event_completed, unit) if event_completed or unit else ""
        else:
            percentage = min(100.0, bar_completed / bar_total * 100) if bar_total else 100.0
            detail = f"{percentage:3.0f}%"
            if event_total is None:
                detail += f"  {_quantity(event_completed, unit)}"
            else:
                detail += (
                    f"  {_quantity(event_completed, unit)} / "
                    f"{_quantity(float(event_total), unit)}"
                )
        observed_rate = task.fields.get("observed_rate")
        rate = float(observed_rate) if observed_rate is not None else task.speed
        if rate is not None:
            detail += f"  {_quantity(rate, unit)}/s"
        remaining = task.fields.get("estimated_remaining_seconds")
        if remaining is not None:
            basis = str(task.fields.get("estimate_basis", "initial"))
            detail += f"  ETA {_duration(float(remaining))} ({basis})"
        return Columns(
            (bar, Text(detail, style="progress.data.speed")),
            padding=(0, 1),
            expand=False,
        )


class ImportProgressRenderer:
    """Render every operation through one Rich task and one terminal line group."""

    def __init__(self, console: Console, *, enabled: bool | None = None) -> None:
        self.enabled = console.is_terminal if enabled is None else enabled
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            _EventProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
            disable=not self.enabled,
        )
        self.task_id: TaskID | None = None
        self.current_phase: str | None = None

    def __enter__(self) -> ImportProgressRenderer:
        self.progress.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.progress.stop()

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        """Temporarily clear the live line while an interactive prompt is shown."""
        self.progress.stop()
        try:
            yield
        finally:
            self.progress.start()

    def update(self, event: ProgressEvent) -> None:
        phase_value = getattr(event.phase, "value", event.phase)
        phase = str(phase_value)
        description = _PHASE_LABELS.get(phase, phase.replace("_", " ").title())
        primitive = getattr(event, "primitive", None)
        field_id = getattr(event, "field_id", None)
        if primitive:
            description += f" {primitive}"
        if field_id is not None:
            description += f" field #{field_id}"
        overall_completed = getattr(event, "overall_completed", None)
        overall_total = getattr(event, "overall_total", None)
        task_completed = event.completed if overall_completed is None else overall_completed
        task_total = event.total if overall_total is None else overall_total
        fields = {
            "event_completed": event.completed,
            "event_total": event.total,
            "uses_overall": overall_total is not None,
            "unit": event.unit,
            "observed_rate": getattr(event, "observed_rate", None),
            "estimated_remaining_seconds": getattr(
                event, "estimated_remaining_seconds", None
            ),
            "estimate_basis": getattr(event, "estimate_basis", "initial"),
        }
        if self.task_id is None:
            self.task_id = self.progress.add_task(
                description,
                total=task_total,
                completed=task_completed,
                **fields,
            )
        elif phase != self.current_phase and overall_total is None:
            self.progress.reset(
                self.task_id,
                completed=task_completed,
                description=description,
                total=task_total,
                **fields,
            )
        else:
            self.progress.update(
                self.task_id,
                completed=task_completed,
                description=description,
                total=task_total,
                **fields,
            )
        self.current_phase = phase
