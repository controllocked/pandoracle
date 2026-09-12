from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from pandoracle import cli, cli_ui
from pandoracle.acceleration_profiles import AccelerationProgress
from pandoracle.cli_ui import ChoiceKind, ImportProgressRenderer, PromptChoice
from pandoracle.errors import ImportFailure
from pandoracle.ingest import (
    ImportProgress,
    MalformedRecord,
    MalformedRecordAction,
    analyze_csv,
)
from pandoracle.models import OperationStatus, SearchClue, SearchOperator, SearchRequest
from pandoracle.schema import confirm_plan
from pandoracle.search import search


class FakePrompts:
    def __init__(
        self,
        selections: list[str | None],
        texts: list[str | None] | None = None,
        passwords: list[str | None] | None = None,
    ) -> None:
        self.selections = iter(selections)
        self.texts = iter(texts or [])
        self.passwords = iter(passwords or [])

    def select(
        self,
        message: str,
        choices: list[PromptChoice],
        *,
        default: str | None = None,
    ) -> str | None:
        del message, choices, default
        return next(self.selections)

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Any = None,
    ) -> str | None:
        del message, default
        value = next(self.texts)
        if value is not None and validate is not None:
            assert validate(value) is True
        return value

    def password(
        self,
        message: str,
        *,
        validate: Any = None,
    ) -> str | None:
        del message
        value = next(self.passwords)
        if value is not None and validate is not None:
            assert validate(value) is True
        return value


def test_main_prints_expected_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise ImportFailure("bad input")

    monkeypatch.setattr(cli, "app", fail)
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "Error: bad input" in captured.err
    assert "Traceback" not in captured.err


def test_main_hides_unexpected_traceback_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise RuntimeError("internal details")

    monkeypatch.setattr(cli, "app", fail)
    monkeypatch.delenv("PANDORACLE_DEBUG", raising=False)
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert "unexpected internal failure" in captured.err
    assert "internal details" not in captured.err
    assert "Traceback" not in captured.err


def test_import_progress_reports_copy_and_rows(tmp_path: Path) -> None:
    source = tmp_path / "people.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    workspace = cli.Workspace.create(tmp_path / "workspace")
    events: list[cli.ImportProgress] = []

    cli.import_csv(
        workspace,
        source,
        schema_plan=confirm_plan(analyze_csv(source)),
        progress=events.append,
    )

    assert any(event.phase.value == "COPYING_RAW" and event.completed > 0 for event in events)
    assert any(event.phase.value == "TRANSFORMING" and event.completed == 1 for event in events)
    assert events[-1].phase.value == "PUBLISHED"


def test_missing_import_source_is_a_domain_error(tmp_path: Path) -> None:
    workspace = cli.Workspace.create(tmp_path / "workspace")
    with pytest.raises(ImportFailure, match="cannot access source file"):
        cli.import_csv(workspace, tmp_path / "missing.csv")


def test_review_requires_explicit_accept_and_does_not_persist_samples(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\nsecret@example.test\n", encoding="utf-8")
    draft = analyze_csv(source)
    prompts = FakePrompts(["0", "__samples__", "__back__", "__confirm__"])

    confirmed = cli._review_schema_plan(draft, {}, prompts=prompts)

    assert confirmed.confirmed
    assert confirmed.fields[0].selected_type == "EMAIL"
    assert "secret@example.test" in capsys.readouterr().out
    assert "secret@example.test" not in str(confirmed.to_dict())


def test_review_edits_a_column_with_select_prompts(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("value\n1234567\n", encoding="utf-8")
    draft = analyze_csv(source)

    confirmed = cli._review_schema_plan(
        draft,
        {},
        prompts=FakePrompts(["0", "PHONE", "__confirm__"]),
    )

    assert confirmed.confirmed
    assert confirmed.fields[0].selected_type == "PHONE"


def test_semantic_type_choices_separate_values_from_actions() -> None:
    choices = cli_ui._type_choices("EMAIL", {}, {})
    by_value = {choice.value: choice for choice in choices}

    assert by_value["EMAIL"].kind is ChoiceKind.DATA
    assert by_value["UNKNOWN"].title.startswith("Skip / leave UNKNOWN")
    assert by_value["UNKNOWN"].kind is ChoiceKind.UTILITY
    assert by_value["__new_type__"].kind is ChoiceKind.UTILITY
    assert by_value["__samples__"].kind is ChoiceKind.UTILITY
    assert by_value["__back__"].kind is ChoiceKind.NAVIGATION


def test_long_questionary_menu_filters_without_jk_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import questionary

    calls: list[dict[str, Any]] = []

    class FakeQuestion:
        def ask(self, *, kbi_msg: str) -> str:
            assert kbi_msg == ""
            return "EMAIL"

    def fake_select(message: str, **kwargs: Any) -> FakeQuestion:
        del message
        calls.append(kwargs)
        return FakeQuestion()

    monkeypatch.setattr(questionary, "select", fake_select)
    custom = {
        f"custom_{index}": cli_ui.CustomTypeSpec(f"custom_{index}", f"Custom {index}")
        for index in range(3)
    }
    choices = cli_ui._type_choices("EMAIL", custom, {})

    selected = cli_ui.QuestionaryPromptBackend().select(
        "Type", choices, default="EMAIL"
    )

    assert selected == "EMAIL"
    assert calls[0]["use_search_filter"] is True
    assert calls[0]["use_jk_keys"] is False
    assert "type to filter" in calls[0]["instruction"]


def test_device_setup_drive_and_credentials_use_shared_prompt_backend() -> None:
    drive = cli.DriveCandidate(
        "/drive", "/block", Path("/dev/fake"), "Synthetic USB", "SERIAL", 2**30, "usb"
    )
    prompts = FakePrompts(["/drive"], passwords=["main passphrase"])

    assert cli._drive_candidate([drive], prompts) is drive
    assert cli._prompt_credential("Main passphrase", prompts) == bytearray(b"main passphrase")


def test_noninteractive_cli_quarantine_reports_both_counts(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email,city\na@example.test,Almaty\nbad\n", encoding="utf-8")
    workspace = cli.Workspace.create(tmp_path / "workspace")
    plan_path = tmp_path / "schema.json"
    cli.write_json_atomic(plan_path, confirm_plan(analyze_csv(source)).to_dict())

    result = CliRunner().invoke(
        cli.app,
        [
            "import",
            str(source),
            "--workspace",
            str(workspace.root),
            "--schema",
            str(plan_path),
            "--on-error",
            "quarantine",
        ],
    )

    assert result.exit_code == 0
    assert "Accepted records: 1" in result.stdout
    assert "Quarantined records: 1" in result.stdout


def test_interactive_malformed_prompt_can_inspect_then_quarantine_similar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    prompt_console = Console(file=output, width=100, color_system=None)
    monkeypatch.setattr(cli, "error_console", prompt_console)
    progress = ImportProgressRenderer(prompt_console, enabled=False)
    record = MalformedRecord(
        source_record_number=7,
        line_start=9,
        line_end=10,
        expected_fields=3,
        actual_fields=2,
        reason="field count mismatch",
        raw_record="synthetic,bad\nrecord\n",
    )

    with progress:
        action = cli._prompt_malformed_record(
            record,
            FakePrompts(["inspect", MalformedRecordAction.QUARANTINE_SIMILAR.value]),
            progress,
        )

    assert action is MalformedRecordAction.QUARANTINE_SIMILAR
    rendered = output.getvalue()
    assert "source record 7 (lines 9-10)" in rendered
    assert "Expected fields: 3; actual: 2" in rendered
    assert "synthetic,bad" in rendered


def test_review_creates_a_validated_custom_type(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("case\nABC-123\n", encoding="utf-8")
    draft = analyze_csv(source)

    confirmed = cli._review_schema_plan(
        draft,
        {},
        prompts=FakePrompts(
            ["0", "__new_type__", "TOKEN", "__confirm__"],
            ["case_id", "Case ID"],
        ),
    )

    assert confirmed.fields[0].selected_type == "case_id"
    assert confirmed.custom_types[0].type_id == "case_id"
    assert confirmed.custom_types[0].default_operator == "TOKEN"


def test_review_cancel_is_a_domain_error(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")

    with pytest.raises(ImportFailure, match="schema review cancelled"):
        cli._review_schema_plan(
            analyze_csv(source),
            {},
            prompts=FakePrompts([None]),
        )


def test_noninteractive_schema_resolution_requires_a_plan(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    with pytest.raises(ImportFailure, match="non-interactive"):
        cli._resolve_cli_schema(analyze_csv(source), None, interactive=False, existing_custom={})


def test_search_table_prints_full_record_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    workspace = cli.Workspace.create(tmp_path / "workspace")
    cli.import_csv(
        workspace,
        source,
        schema_plan=confirm_plan(analyze_csv(source)),
    )
    result = search(workspace, SearchRequest((SearchClue("EMAIL", "a@example.test"),)))
    hit = result.records[0]
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, width=240, color_system=None))

    cli._emit_search_result(result, explain=False)

    assert f"pandoracle inspect '{hit.ref.external_id}'" in output.getvalue()


def test_shell_explains_and_reports_automatic_search_scope(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    workspace = cli.Workspace.create(tmp_path / "workspace")
    cli.import_csv(
        workspace,
        source,
        schema_plan=confirm_plan(analyze_csv(source)),
    )

    result = CliRunner().invoke(
        cli.app,
        ["shell", "--workspace", str(workspace.root)],
        input="a@example.test\n:help\n:quit\n",
    )

    assert result.exit_code == 0
    output = " ".join(result.stdout.split())
    assert "Enter one value for a quick search" in output
    assert "fields tagged with that type in every active dataset" in output
    assert "Quick search: inferred EMAIL; AUTO -> EXACT; scope: all active datasets." in output
    assert "Private search shell. Type :help for help." in output
    assert "Import and maintenance are ordinary pandoracle commands" in output
    assert output.count("Commands: :search, :datasets, :types, :help, :quit") == 1


def test_prompt_clue_describes_and_validates_exact_date(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("birth_date\n2001-02-03\n", encoding="utf-8")
    workspace = cli.Workspace.create(tmp_path / "workspace")
    cli.import_csv(
        workspace,
        source,
        schema_plan=confirm_plan(analyze_csv(source), {0: "DATE_OF_BIRTH"}),
    )

    class InspectingPrompts(FakePrompts):
        operator_titles: list[str]
        invalid_feedback: str

        def select(
            self,
            message: str,
            choices: list[PromptChoice],
            *,
            default: str | None = None,
        ) -> str | None:
            if message == "Operator":
                self.operator_titles = [choice.title for choice in choices]
            return super().select(message, choices, default=default)

        def text(
            self,
            message: str,
            *,
            default: str = "",
            validate: Any = None,
        ) -> str | None:
            assert validate is not None
            self.invalid_feedback = validate("2001")
            return super().text(message, default=default, validate=validate)

    prompts = InspectingPrompts(
        ["DATE_OF_BIRTH", SearchOperator.EXACT.value], ["2001-02-03"]
    )
    clue = cli._prompt_clue(workspace, None, prompts)

    assert clue == SearchClue("DATE_OF_BIRTH", "2001-02-03", SearchOperator.EXACT)
    assert "Automatic (complete date -> EXACT; year -> RANGE)" in prompts.operator_titles
    assert "Exact date (YYYY-MM-DD, DD.MM.YYYY, or DD/MM/YYYY)" in prompts.operator_titles
    assert "expected a complete date" in prompts.invalid_feedback


def test_guided_search_builds_multiple_validated_clues(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("full_name,birth_date\nVadim Li,2008-02-03\n", encoding="utf-8")
    workspace = cli.Workspace.create(tmp_path / "workspace")
    cli.import_csv(
        workspace,
        source,
        schema_plan=confirm_plan(
            analyze_csv(source), {0: "PERSON_NAME", 1: "DATE_OF_BIRTH"}
        ),
    )
    prompts = FakePrompts(
        [
            "__all__",
            "PERSON_NAME",
            SearchOperator.TOKEN.value,
            "add",
            "DATE_OF_BIRTH",
            SearchOperator.AUTO.value,
            "run",
        ],
        ["Vadim", "2008"],
    )

    request = cli._prompt_candidate_query(workspace, prompts)

    assert request == SearchRequest(
        (
            SearchClue("PERSON_NAME", "Vadim", SearchOperator.TOKEN),
            SearchClue("DATE_OF_BIRTH", "2008", SearchOperator.AUTO),
        )
    )


def test_progress_renderer_reuses_one_task_and_only_shows_exact_percent() -> None:
    output = StringIO()
    progress_console = Console(file=output, width=120, color_system=None, force_terminal=True)
    renderer = ImportProgressRenderer(progress_console, enabled=True)

    with renderer:
        renderer.update(ImportProgress(OperationStatus.COPYING_RAW, 50, 100, "bytes"))
        task_id = renderer.task_id
        determined = StringIO()
        Console(file=determined, width=120, color_system=None).print(
            renderer.progress.get_renderable()
        )
        renderer.update(ImportProgress(OperationStatus.TRANSFORMING, 10, None, "rows"))
        indeterminate = StringIO()
        Console(file=indeterminate, width=120, color_system=None).print(
            renderer.progress.get_renderable()
        )

    assert renderer.task_id == task_id
    assert len(renderer.progress.tasks) == 1
    assert "50%" in determined.getvalue()
    assert "%" not in indeterminate.getvalue()
    assert "10 rows" in indeterminate.getvalue()


def test_progress_renderer_is_silent_when_not_interactive() -> None:
    output = StringIO()
    renderer = ImportProgressRenderer(
        Console(file=output, color_system=None, force_terminal=False),
        enabled=False,
    )

    with renderer:
        renderer.update(ImportProgress(OperationStatus.TRANSFORMING, 1, None, "rows"))

    assert output.getvalue() == ""


def test_acceleration_progress_keeps_whole_build_bar_and_observed_eta() -> None:
    output = StringIO()
    progress_console = Console(file=output, width=160, color_system=None, force_terminal=True)
    renderer = ImportProgressRenderer(progress_console, enabled=True)

    with renderer:
        renderer.update(
            AccelerationProgress(
                "BUILDING",
                200,
                1_000,
                primitive="TOKEN",
                field_id=0,
                overall_completed=4.0,
                overall_total=20.0,
                observed_rate=50.0,
                estimated_remaining_seconds=16.0,
                estimate_basis="observed",
            )
        )
        renderer.update(
            AccelerationProgress(
                "FINALIZING",
                1_000,
                1_000,
                primitive="TOKEN",
                overall_completed=18.0,
                overall_total=20.0,
                estimated_remaining_seconds=2.0,
                estimate_basis="observed",
            )
        )
        rendered = StringIO()
        Console(file=rendered, width=160, color_system=None).print(
            renderer.progress.get_renderable()
        )

    text = rendered.getvalue()
    assert len(renderer.progress.tasks) == 1
    assert "90%" in text
    assert "1,000 rows / 1,000 rows" in text
    assert "ETA 0:02 (observed)" in text


def test_search_json_v1_contract_and_optional_explain(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    workspace = cli.Workspace.create(tmp_path / "workspace")
    cli.import_csv(
        workspace,
        source,
        schema_plan=confirm_plan(analyze_csv(source)),
    )
    runner = CliRunner()

    ordinary = runner.invoke(
        cli.app,
        ["search", "a@example.test", "--workspace", str(workspace.root), "--format", "json"],
    )
    explained = runner.invoke(
        cli.app,
        [
            "search",
            "a@example.test",
            "--workspace",
            str(workspace.root),
            "--format",
            "json",
            "--explain",
        ],
    )

    import json

    ordinary_value = json.loads(ordinary.stdout)
    assert set(ordinary_value) == {"result_version", "query", "records", "truncated"}
    assert ordinary_value["result_version"] == 1
    value = json.loads(explained.stdout)
    assert set(value) == {"result_version", "query", "records", "truncated", "execution"}
    assert value["execution"]["execution_version"] == 1


def test_acceleration_commands_configure_without_changing_record_ref(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    workspace = cli.Workspace.create(tmp_path / "workspace")
    imported = cli.import_csv(
        workspace,
        source,
        dataset_name="people",
        schema_plan=confirm_plan(analyze_csv(source)),
    )
    plan_path = tmp_path / "acceleration.json"
    runner = CliRunner()
    planned = runner.invoke(
        cli.app,
        [
            "acceleration",
            "plan",
            "people",
            "--workspace",
            str(workspace.root),
            "--output",
            str(plan_path),
        ],
    )
    assert planned.exit_code == 0
    value = cli.load_acceleration_plan(plan_path)
    cli.write_json_atomic(
        plan_path,
        cli.confirm_acceleration_plan(
            value,
            {
                field.field_id: ((cli.AccelerationKind.VALUE,) if field.field_id == 0 else ())
                for field in value.fields
            },
        ).to_dict(),
    )

    revised = runner.invoke(
        cli.app,
        [
            "acceleration",
            "configure",
            "people",
            "--workspace",
            str(workspace.root),
            "--plan",
            str(plan_path),
            "--format",
            "json",
        ],
    )

    assert revised.exit_code == 0
    import json

    result = json.loads(revised.stdout)
    assert result["dataset_version_id"] == imported.dataset_version_id
    assert result["posting_count"] == 1


def test_removed_index_command_is_not_aliased() -> None:
    result = CliRunner().invoke(cli.app, ["index", "--help"])

    assert result.exit_code != 0
    assert "No such command" in result.output


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["--help"], "recommended private interactive search experience"),
        (["search", "--help"], "JSON includes complete records and provenance"),
        (["shell", "--help"], "Bare input is a quick search"),
        (["import", "--help"], "confirmed plan"),
        (["schema", "review", "--help"], "Use arrows or j/k"),
        (["schema", "revise", "--help"], "previous active version"),
        (["inspect", "--help"], "complete record with provenance"),
        (["maintenance", "verify", "--help"], "hashes every registered RAW"),
        (["maintenance", "recover", "--help"], "no writer process is active"),
        (["maintenance", "gc", "--help"], "non-mutating dry run"),
        (["acceleration", "--help"], "Acceleration is optional"),
    ],
)
def test_help_explains_key_workflows(arguments: list[str], expected: str) -> None:
    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code == 0
    assert expected in " ".join(result.stdout.split())
