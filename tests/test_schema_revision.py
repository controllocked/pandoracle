import contextlib
from pathlib import Path

import pytest

import pandoracle.revision as revision_module
from pandoracle.errors import ImportFailure
from pandoracle.ingest import analyze_csv, import_csv
from pandoracle.models import SearchClue, SearchRequest
from pandoracle.revision import analyze_dataset, revise_dataset
from pandoracle.schema import CustomTypeSpec, confirm_plan
from pandoracle.search import inspect_record, search
from pandoracle.workspace import Workspace


def test_inference_is_not_publishable_without_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    draft = analyze_csv(source)

    assert not draft.confirmed
    assert draft.fields[0].selected_type is None
    with pytest.raises(ImportFailure, match="fully confirmed"):
        import_csv(workspace, source, schema_plan=draft)
    assert not any((workspace.root / "objects/raw/sha256").iterdir())


def test_pre_v1_mapping_is_rejected_before_raw_copy(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email,note\na@example.test,x\n", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text('{"email": "EMAIL"}', encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")

    with pytest.raises(ImportFailure, match="schema analyze"):
        import_csv(workspace, source, schema_path=schema)
    assert not any((workspace.root / "objects/raw/sha256").iterdir())


def test_obsolete_versioned_schema_plan_is_rejected_with_regeneration_guidance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    value = confirm_plan(analyze_csv(source)).to_dict()
    value["schema_plan_version"] = 2

    from pandoracle.schema import SchemaPlan

    with pytest.raises(ImportFailure, match="regenerate.*schema analyze"):
        SchemaPlan.from_dict(value)


def test_custom_type_is_registered_and_found_by_universal_search(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text("customer_code,note\n AbC-42 ,first\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    custom = CustomTypeSpec("customer_code", "Customer code")
    plan = confirm_plan(
        analyze_csv(source),
        {0: "customer_code", 1: "UNKNOWN"},
        custom_types=(custom,),
    )

    imported = import_csv(workspace, source, schema_plan=plan)

    assert workspace.catalog.list_custom_types() == {"customer_code": custom}
    result = search(workspace, SearchRequest((SearchClue("customer_code", "abc-42"),)))
    assert len(result.records) == 1
    assert result.records[0].matches[0].semantic_type == "customer_code"
    dataset = workspace.catalog.find_dataset(imported.dataset_id)
    assert dataset is not None
    assert dataset["canonical_fields"][0]["canonicalizer_id"] == "exact-text/v1"


def test_schema_fingerprint_prevents_wrong_source_only_dedup(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("full_name,email\nAlex Smith,a@example.test\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    draft = analyze_csv(source)
    untyped_name = confirm_plan(draft, {0: "UNKNOWN", 1: "EMAIL"})
    typed_name = confirm_plan(draft, {0: "PERSON_NAME", 1: "EMAIL"})

    first = import_csv(workspace, source, dataset_name="people", schema_plan=untyped_name)
    second = import_csv(workspace, source, dataset_name="people", schema_plan=typed_name)

    assert not second.deduplicated
    assert second.dataset_version_id != first.dataset_version_id
    assert len(list((workspace.root / "objects/raw/sha256").iterdir())) == 1


def test_revision_reuses_raw_and_preserves_old_record_refs(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "ФИО,Дата рождения,ИНН,Телефон\n"
        "Ли Вадим Владимирович,1990-02-14,123456789012,+77001234567\n",
        encoding="utf-8",
    )
    workspace = Workspace.create(tmp_path / "workspace")
    draft = analyze_csv(source)
    legacy = confirm_plan(
        draft,
        {0: "UNKNOWN", 1: "DATE", 2: "UNKNOWN", 3: "PHONE"},
    )
    first = import_csv(workspace, source, dataset_name="Kazakhstan", schema_plan=legacy)
    old_ref = f"{first.dataset_version_id}:0"

    revised_plan = confirm_plan(analyze_dataset(workspace, "Kazakhstan"))
    progress_events = []
    second = revise_dataset(
        workspace,
        "Kazakhstan",
        revised_plan,
        progress=progress_events.append,
    )

    assert second.dataset_version_id != first.dataset_version_id
    assert len(list((workspace.root / "objects/raw/sha256").iterdir())) == 1
    assert inspect_record(workspace, old_ref)["record"]["ФИО"] == "Ли Вадим Владимирович"
    result = search(
        workspace,
        SearchRequest((SearchClue("PERSON_NAME", "Ли Вадим Владимирович"),), dataset="Kazakhstan"),
    )
    assert result.records[0].matches[0].mode.value == "TOKEN"
    transform_events = [event for event in progress_events if event.phase.value == "TRANSFORMING"]
    assert transform_events
    assert all(event.total == first.row_count for event in transform_events)
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        row = connection.execute(
            "SELECT parent_version_id FROM dataset_versions WHERE id = ?",
            (second.dataset_version_id,),
        ).fetchone()
    assert row[0] == first.dataset_version_id


def test_failed_revision_keeps_old_version_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.csv"
    source.write_text("full_name\nAlex Smith\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    first = import_csv(
        workspace,
        source,
        dataset_name="people",
        schema_plan=confirm_plan(analyze_csv(source), {0: "UNKNOWN"}),
    )
    revised = confirm_plan(analyze_dataset(workspace, "people"), {0: "PERSON_NAME"})

    def fail(*args, **kwargs):
        raise RuntimeError("injected build failure")

    monkeypatch.setattr(revision_module, "_build_artifacts", fail)
    with pytest.raises(ImportFailure, match="revision failed"):
        revise_dataset(workspace, "people", revised)

    dataset = workspace.catalog.find_dataset("people")
    assert dataset is not None
    assert dataset["active_version_id"] == first.dataset_version_id


def test_conflicting_custom_type_is_rejected_before_raw_copy(tmp_path: Path) -> None:
    first_source = tmp_path / "first.csv"
    first_source.write_text("value\nabc\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    first_type = CustomTypeSpec("case_id", "Case ID")
    import_csv(
        workspace,
        first_source,
        schema_plan=confirm_plan(
            analyze_csv(first_source), {0: "case_id"}, custom_types=(first_type,)
        ),
    )

    second_source = tmp_path / "second.csv"
    second_source.write_text("value\ndef\n", encoding="utf-8")
    conflicting = CustomTypeSpec("case_id", "Different definition")
    before = set((workspace.root / "objects/raw/sha256").iterdir())
    with pytest.raises(ImportFailure, match="conflicts"):
        import_csv(
            workspace,
            second_source,
            schema_plan=confirm_plan(
                analyze_csv(second_source),
                {0: "case_id"},
                custom_types=(conflicting,),
            ),
        )
    assert set((workspace.root / "objects/raw/sha256").iterdir()) == before
