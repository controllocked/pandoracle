from pathlib import Path

import pytest
from typer.testing import CliRunner

from pandoracle import cli, cli_ui
from pandoracle.errors import ImportFailure
from pandoracle.ingest import analyze_csv, import_csv
from pandoracle.models import SearchClue, SearchRequest
from pandoracle.revision import analyze_dataset, revise_dataset
from pandoracle.schema import CustomTypeSpec, confirm_plan
from pandoracle.search import search
from pandoracle.workspace import Workspace


def _custom_workspace(tmp_path: Path) -> tuple[Workspace, CustomTypeSpec]:
    source = tmp_path / "source.csv"
    source.write_text("code,note\n AbC-42 ,first\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    custom = CustomTypeSpec("customer_code", "Customer code")
    import_csv(
        workspace,
        source,
        dataset_name="people",
        schema_plan=confirm_plan(
            analyze_csv(source),
            {0: custom.type_id, 1: "UNKNOWN"},
            custom_types=(custom,),
        ),
    )
    return workspace, custom


def test_retire_restore_is_idempotent_and_builtins_are_fixed(tmp_path: Path) -> None:
    workspace, custom = _custom_workspace(tmp_path)

    assert workspace.catalog.set_custom_type_retired(custom.type_id, retired=True)
    assert not workspace.catalog.set_custom_type_retired(custom.type_id, retired=True)
    assert custom.type_id not in workspace.catalog.list_custom_types(include_retired=False)
    assert workspace.catalog.list_custom_type_rows()[0]["retired_at"] is not None
    assert workspace.catalog.set_custom_type_retired(custom.type_id, retired=False)
    assert not workspace.catalog.set_custom_type_retired(custom.type_id, retired=False)

    result = CliRunner().invoke(
        cli.app,
        ["types", "retire", "EMAIL", "--workspace", str(workspace.root)],
    )
    assert result.exit_code != 0
    assert "built-in semantic types cannot be retired" in str(result.exception)


def test_retired_type_remains_searchable_and_can_stay_on_same_field(
    tmp_path: Path,
) -> None:
    workspace, custom = _custom_workspace(tmp_path)
    workspace.catalog.set_custom_type_retired(custom.type_id, retired=True)

    result = search(workspace, SearchRequest((SearchClue(custom.type_id, "abc-42"),)))
    assert [item.record["code"] for item in result.records] == [" AbC-42 "]

    draft = analyze_dataset(workspace, "people")
    assert draft.fields[0].selected_type == custom.type_id
    retained = confirm_plan(
        draft,
        {0: custom.type_id, 1: "UNKNOWN"},
        custom_types=(),
    )
    result = revise_dataset(workspace, "people", retained)
    assert result.deduplicated


def test_retired_type_cannot_be_imported_or_moved_until_restored(tmp_path: Path) -> None:
    workspace, custom = _custom_workspace(tmp_path)
    workspace.catalog.set_custom_type_retired(custom.type_id, retired=True)

    second = tmp_path / "second.csv"
    second.write_text("code\nXYZ-7\n", encoding="utf-8")
    blocked_import = confirm_plan(
        analyze_csv(second),
        {0: custom.type_id},
        custom_types=(custom,),
    )
    with pytest.raises(ImportFailure, match="retired.*pandoracle types restore customer_code"):
        import_csv(workspace, second, dataset_name="second", schema_plan=blocked_import)

    draft = analyze_dataset(workspace, "people")
    moved = confirm_plan(
        draft,
        {0: custom.type_id, 1: custom.type_id},
        custom_types=(),
    )
    with pytest.raises(ImportFailure, match="retired.*pandoracle types restore customer_code"):
        revise_dataset(workspace, "people", moved)

    workspace.catalog.set_custom_type_retired(custom.type_id, retired=False)
    imported = import_csv(workspace, second, dataset_name="second", schema_plan=blocked_import)
    assert imported.row_count == 1


def test_retired_type_is_hidden_from_new_review_choices(tmp_path: Path) -> None:
    workspace, custom = _custom_workspace(tmp_path)
    workspace.catalog.set_custom_type_retired(custom.type_id, retired=True)
    active = workspace.catalog.list_custom_types(include_retired=False)

    choices = cli_ui._type_choices("UNKNOWN", active, {})
    assert custom.type_id not in {choice.value for choice in choices}
    retained_choices = cli_ui._type_choices(custom.type_id, active, {})
    assert custom.type_id in {choice.value for choice in retained_choices}
