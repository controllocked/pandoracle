from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pandoracle.acceleration_profiles import (
    AccelerationProgress,
    configure_acceleration,
    confirm_acceleration_plan,
    disable_acceleration,
    make_acceleration_plan,
    rebuild_acceleration,
    show_acceleration,
)
from pandoracle.contracts import builtin_contract
from pandoracle.errors import SearchFailure
from pandoracle.ingest import analyze_csv, import_csv
from pandoracle.maintenance import apply_gc, plan_gc, verify_workspace
from pandoracle.models import AccelerationKind, SearchClue, SearchOperator, SearchRequest
from pandoracle.schema import CustomTypeSpec, confirm_plan
from pandoracle.universal_search import execute_search, validate_search_clue
from pandoracle.workspace import Workspace


def _import_people(tmp_path: Path) -> tuple[Workspace, object]:
    source = tmp_path / "people.csv"
    source.write_text(
        "full_name,alias,birth_date,email\n"
        "Вадим Ли Сергеевич,North Star,2008-02-03,vadim@example.test\n"
        "Анна Ли,South Star,2007-07-08,anna@example.test\n"
        "Other Person,Вадим Ли,2010-01-01,other@example.test\n",
        encoding="utf-8",
    )
    workspace = Workspace.create(tmp_path / "workspace")
    plan = confirm_plan(
        analyze_csv(source),
        {0: "PERSON_NAME", 1: "PERSON_NAME", 2: "DATE_OF_BIRTH", 3: "EMAIL"},
    )
    return workspace, import_csv(workspace, source, dataset_name="people", schema_plan=plan)


@pytest.mark.parametrize(
    ("semantic_type", "valid", "invalid"),
    [
        ("EMAIL", "person@example.test", "not-an-email"),
        ("PHONE", "+7 700 000 00 42", "123"),
        ("DOMAIN", "example.test", "not a domain"),
        ("URL", "https://example.test/path", "example.test/path"),
        ("USERNAME", "some_user", "two users"),
        ("IP", "2001:db8::1", "999.999.999.999"),
        ("DATE", "2001-02-03", "2001"),
        ("DATE_OF_BIRTH", "03.02.2001", "2001"),
        ("PERSON_NAME", "Synthetic Person", "12345"),
    ],
)
def test_clue_validation_covers_all_builtin_search_types(
    semantic_type: str, valid: str, invalid: str
) -> None:
    contract = builtin_contract(semantic_type)

    validate_search_clue(SearchClue(semantic_type, valid, SearchOperator.EXACT), contract)
    with pytest.raises(SearchFailure, match=f"query is not valid for {semantic_type}; expected"):
        validate_search_clue(SearchClue(semantic_type, invalid, SearchOperator.EXACT), contract)


def test_date_clue_validation_distinguishes_exact_auto_and_range() -> None:
    contract = builtin_contract("DATE_OF_BIRTH")

    validate_search_clue(SearchClue("DATE_OF_BIRTH", "2001"), contract)
    validate_search_clue(
        SearchClue("DATE_OF_BIRTH", "2001..2003", SearchOperator.RANGE), contract
    )
    validate_search_clue(
        SearchClue(
            "DATE_OF_BIRTH",
            "2001-01-01..2003-12-31",
            SearchOperator.RANGE,
        ),
        contract,
    )
    with pytest.raises(SearchFailure, match="complete date"):
        validate_search_clue(
            SearchClue("DATE_OF_BIRTH", "2001", SearchOperator.EXACT), contract
        )


def test_exact_birth_year_execution_explains_how_to_search_a_year(tmp_path: Path) -> None:
    workspace, _ = _import_people(tmp_path)

    with pytest.raises(
        SearchFailure,
        match=r"expected a complete date .* use AUTO or RANGE for YYYY",
    ):
        execute_search(
            workspace,
            SearchRequest(
                (SearchClue("DATE_OF_BIRTH", "2008", SearchOperator.EXACT),)
            ),
        )


def test_scan_only_auto_token_range_and_full_provenance(tmp_path: Path) -> None:
    workspace, imported = _import_people(tmp_path)

    result = execute_search(
        workspace,
        SearchRequest(
            (
                SearchClue("PERSON_NAME", "ли вадим"),
                SearchClue("DATE_OF_BIRTH", "2008"),
            )
        ),
    )

    assert imported.posting_count == 0
    assert [item.record["full_name"] for item in result.records] == ["Вадим Ли Сергеевич"]
    assert [item.mode for item in result.records[0].matches] == [
        SearchOperator.TOKEN,
        SearchOperator.RANGE,
    ]
    assert result.records[0].matches[0].provenances[0].original_value == ("Вадим Ли Сергеевич")
    assert result.execution["segments"][0]["path"] == "SCAN"

    explicit_exact = execute_search(
        workspace,
        SearchRequest((SearchClue("PERSON_NAME", "Вадим Ли Сергеевич", SearchOperator.EXACT),)),
    )
    date_range = execute_search(
        workspace,
        SearchRequest(
            (
                SearchClue(
                    "DATE_OF_BIRTH",
                    "2007-07-08..2008-02-03",
                    SearchOperator.RANGE,
                ),
            )
        ),
    )
    duplicate_tokens = execute_search(
        workspace,
        SearchRequest((SearchClue("PERSON_NAME", "ли ли вадим"),)),
    )
    assert [item.record["email"] for item in explicit_exact.records] == ["vadim@example.test"]
    assert [item.record["email"] for item in date_range.records] == [
        "vadim@example.test",
        "anna@example.test",
    ]
    assert [item.record["email"] for item in duplicate_tokens.records] == [
        "vadim@example.test",
        "other@example.test",
    ]


def test_value_token_acceleration_and_scan_flag_does_not_force_scan(tmp_path: Path) -> None:
    workspace, imported = _import_people(tmp_path)
    plan = make_acceleration_plan(workspace, "people")
    selections = {
        field.field_id: tuple(
            primitive
            for primitive in (AccelerationKind.VALUE, AccelerationKind.TOKEN)
            if any(
                estimate.field_id == field.field_id and estimate.primitive is primitive
                for estimate in plan.estimates
            )
        )
        for field in plan.fields
    }
    published = configure_acceleration(
        workspace, "people", confirm_acceleration_plan(plan, selections)
    )

    result = execute_search(
        workspace,
        SearchRequest(
            (SearchClue("EMAIL", "anna@example.test"),),
            allow_expensive_scan=True,
        ),
    )
    token = execute_search(
        workspace,
        SearchRequest((SearchClue("PERSON_NAME", "анна ли"),)),
    )

    assert published.dataset_version_id == imported.dataset_version_id
    assert result.records[0].record["full_name"] == "Анна Ли"
    assert result.execution["segments"][0]["path"] == "VALUE"
    assert token.execution["segments"][0]["path"] == "TOKEN"
    assert result.execution["segments"][0]["clues"][0]["access"] == "VALUE"

    first = execute_search(
        workspace,
        SearchRequest(
            (
                SearchClue("PERSON_NAME", "ли"),
                SearchClue("EMAIL", "anna@example.test"),
            )
        ),
    )
    reversed_request = execute_search(
        workspace,
        SearchRequest(
            (
                SearchClue("EMAIL", "anna@example.test"),
                SearchClue("PERSON_NAME", "ли"),
            )
        ),
    )
    assert [item.ref for item in first.records] == [item.ref for item in reversed_request.records]
    assert (
        first.execution["segments"][0]["steps"]
        == reversed_request.execution["segments"][0]["steps"]
    )


def test_partial_same_type_acceleration_scans_without_false_negatives(tmp_path: Path) -> None:
    workspace, _ = _import_people(tmp_path)
    plan = make_acceleration_plan(workspace, "people")
    configured = confirm_acceleration_plan(
        plan,
        {
            field.field_id: ((AccelerationKind.TOKEN,) if field.field_id == 0 else ())
            for field in plan.fields
        },
    )
    configure_acceleration(workspace, "people", configured)

    result = execute_search(workspace, SearchRequest((SearchClue("PERSON_NAME", "вадим ли"),)))

    assert [item.record["email"] for item in result.records] == [
        "vadim@example.test",
        "other@example.test",
    ]
    clue = result.execution["segments"][0]["clues"][0]
    assert clue["acceleration_coverage"] == "partial"
    assert clue["access"] == "SCAN"


def test_custom_auto_default_token_across_heterogeneous_datasets(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    custom = CustomTypeSpec("label", "Label", default_operator="TOKEN")
    for number, header in enumerate(("left_label", "description"), start=1):
        source = tmp_path / f"source-{number}.csv"
        source.write_text(f"{header}\nAlpha\u00a0Beta-Gamma\n", encoding="utf-8")
        import_csv(
            workspace,
            source,
            dataset_name=f"dataset-{number}",
            schema_plan=confirm_plan(analyze_csv(source), {0: "label"}, custom_types=(custom,)),
        )

    result = execute_search(workspace, SearchRequest((SearchClue("label", "BETA-GAMMA\talpha"),)))

    assert [item.dataset_name for item in result.records] == ["dataset-1", "dataset-2"]
    assert all(item.matches[0].mode is SearchOperator.TOKEN for item in result.records)
    estimate = next(
        item
        for item in make_acceleration_plan(workspace, "dataset-1").estimates
        if item.primitive is AccelerationKind.TOKEN
    )
    assert estimate.utility_tier == "custom"


def test_legacy_canonical_statistics_use_a_deterministic_token_sample(tmp_path: Path) -> None:
    workspace, imported = _import_people(tmp_path)
    with workspace.catalog.transaction() as connection:
        row = connection.execute(
            "SELECT statistics_json FROM canonical_profiles WHERE id=?",
            (imported.canonical_profile_id,),
        ).fetchone()
        statistics = json.loads(row[0])
        for value in statistics.values():
            for key in (
                "canonical_utf8_bytes",
                "estimated_token_postings",
                "estimated_token_key_bytes",
                "token_sample_count",
                "token_sample_postings",
                "token_sample_key_bytes",
            ):
                value.pop(key, None)
        connection.execute(
            "UPDATE canonical_profiles SET statistics_json=? WHERE id=?",
            (json.dumps(statistics, sort_keys=True), imported.canonical_profile_id),
        )

    plan = make_acceleration_plan(workspace, "people")
    full_name_value = next(
        item
        for item in plan.estimates
        if item.field_id == 0 and item.primitive is AccelerationKind.VALUE
    )
    full_name_token = next(
        item
        for item in plan.estimates
        if item.field_id == 0 and item.primitive is AccelerationKind.TOKEN
    )

    assert full_name_value.posting_count == 3
    assert full_name_token.posting_count == 7
    assert full_name_token.token_sample_count == 3
    assert "TOKEN sample" in full_name_token.basis
    assert full_name_token.key_bytes != full_name_value.key_bytes


def test_import_token_sample_is_not_biased_to_the_first_values(tmp_path: Path) -> None:
    source = tmp_path / "ordered.csv"
    source.write_text(
        "label\n"
        + "Alpha\n" * 4_096
        + "Beta Gamma Delta\n" * 904,
        encoding="utf-8",
    )
    workspace = Workspace.create(tmp_path / "workspace")
    custom = CustomTypeSpec("label", "Label", default_operator="TOKEN")
    import_csv(
        workspace,
        source,
        dataset_name="ordered",
        schema_plan=confirm_plan(
            analyze_csv(source),
            {0: "label"},
            custom_types=(custom,),
        ),
    )

    plan = make_acceleration_plan(workspace, "ordered")
    value = next(
        item for item in plan.estimates if item.primitive is AccelerationKind.VALUE
    )
    token = next(
        item for item in plan.estimates if item.primitive is AccelerationKind.TOKEN
    )

    assert value.posting_count == 5_000
    assert token.token_sample_count == 4_096
    assert token.posting_count > value.posting_count


def test_acceleration_records_phase_metrics_and_observed_eta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _import_people(tmp_path)
    monkeypatch.setattr("pandoracle.acceleration_profiles.BUILD_BATCH_ROWS", 1)
    plan = make_acceleration_plan(workspace, "people")
    configured = confirm_acceleration_plan(
        plan,
        {
            field.field_id: (
                (AccelerationKind.TOKEN,)
                if field.field_id == 0
                else (AccelerationKind.VALUE,)
                if field.field_id == 3
                else ()
            )
            for field in plan.fields
        },
    )
    events: list[AccelerationProgress] = []

    configure_acceleration(workspace, "people", configured, progress=events.append)
    profile = show_acceleration(workspace, "people")[0]
    metrics = profile["build_metrics"]

    assert metrics["metrics_version"] == 1
    assert metrics["phases"]["normalization_seconds"] == 0.0
    assert metrics["phases"]["canonical_read_seconds"] >= 0.0
    assert metrics["phases"]["tokenization_seconds"] >= 0.0
    assert metrics["phases"]["posting_generation_seconds"] >= 0.0
    assert metrics["phases"]["sqlite_write_seconds"] >= 0.0
    assert metrics["phases"]["index_btree_finalization_seconds"] >= 0.0
    assert metrics["phases"]["validation_seconds"] >= 0.0
    assert metrics["phases"]["filesystem_publication_seconds"] >= 0.0
    assert metrics["phases"]["catalog_publication_seconds"] >= 0.0
    assert set(metrics["field_metrics"]) == {"TOKEN:0", "VALUE:3"}
    assert metrics["field_metrics"]["TOKEN:0"]["posting_count"] == 7
    assert metrics["primitive_metrics"]["TOKEN"]["btree_maintenance"] == (
        "inline_in_sqlite_write"
    )
    assert any(event.estimate_basis == "observed" for event in events)
    assert events[-1].phase == "PUBLISHED"
    assert events[-1].overall_completed == events[-1].overall_total


def test_lifecycle_deduplicate_rebuild_clear_and_history(tmp_path: Path) -> None:
    workspace, imported = _import_people(tmp_path)
    plan = make_acceleration_plan(workspace, "people")
    selected = confirm_acceleration_plan(
        plan,
        {
            field.field_id: ((AccelerationKind.VALUE,) if field.semantic_type == "EMAIL" else ())
            for field in plan.fields
        },
    )
    first = configure_acceleration(workspace, "people", selected)
    duplicate = configure_acceleration(workspace, "people", selected)
    rebuilt = rebuild_acceleration(workspace, "people")
    disabled = disable_acceleration(workspace, "people")

    assert duplicate.deduplicated
    assert rebuilt.index_profile_id != first.index_profile_id
    assert disabled.dataset_version_id == imported.dataset_version_id
    assert disabled.posting_count == 0
    assert len(show_acceleration(workspace, "people", history=True)) == 4
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE dataset_version_id=? "
                "AND kind='RECORD_PARQUET'",
                (imported.dataset_version_id,),
            ).fetchone()[0]
            == 1
        )

    gc_plan = plan_gc(workspace)
    assert gc_plan.blockers == ()
    apply_gc(workspace)
    history = show_acceleration(workspace, "people", history=True)
    assert history[0]["status"] == "PUBLISHED"
    assert {item["status"] for item in history[1:]} == {"RETIRED"}
    assert verify_workspace(workspace)["artifacts"] == 2


def test_expensive_scan_permission_is_preflighted_and_scan_flag_authorizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _import_people(tmp_path)
    monkeypatch.setattr("pandoracle.universal_search._SCAN_PERMISSION_SECONDS", -1.0)
    request = SearchRequest((SearchClue("EMAIL", "anna@example.test"),))

    with pytest.raises(SearchFailure, match="estimated.*rerun with --scan"):
        execute_search(workspace, request)
    allowed = execute_search(workspace, replace(request, allow_expensive_scan=True))

    assert allowed.records[0].record["email"] == "anna@example.test"


def test_invalid_or_unsearchable_semantic_type_is_rejected(tmp_path: Path) -> None:
    workspace, _ = _import_people(tmp_path)

    with pytest.raises(SearchFailure, match="unknown semantic type: missing"):
        execute_search(workspace, SearchRequest((SearchClue("missing", "value"),)))
    with pytest.raises(SearchFailure, match="UNKNOWN is not searchable"):
        execute_search(workspace, SearchRequest((SearchClue("UNKNOWN", "value"),)))


def test_oversized_secondary_acceleration_keeps_bounded_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _import_people(tmp_path)
    plan = make_acceleration_plan(workspace, "people")
    configured = confirm_acceleration_plan(
        plan,
        {
            field.field_id: tuple(
                primitive
                for primitive in (AccelerationKind.VALUE, AccelerationKind.TOKEN)
                if any(
                    estimate.field_id == field.field_id and estimate.primitive is primitive
                    for estimate in plan.estimates
                )
            )
            for field in plan.fields
        },
    )
    configure_acceleration(workspace, "people", configured)
    monkeypatch.setattr("pandoracle.universal_search.MAX_ROWSET_ORDINALS", 2)
    monkeypatch.setattr("pandoracle.universal_search._CANDIDATE_FILTER_ROWS", 0)

    result = execute_search(
        workspace,
        SearchRequest(
            (
                SearchClue("PERSON_NAME", "star"),
                SearchClue("DATE_OF_BIRTH", "2007..2010"),
            )
        ),
    )

    assert [item.record["email"] for item in result.records] == [
        "vadim@example.test",
        "anna@example.test",
    ]
    assert result.execution["segments"][0]["rowset_overflow"] is False


def test_scan_permission_aggregates_dataset_fanout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    custom = CustomTypeSpec("label", "Label", default_operator="TOKEN")
    for number in (1, 2):
        source = tmp_path / f"source-{number}.csv"
        source.write_text("label\nAlpha Beta\n", encoding="utf-8")
        import_csv(
            workspace,
            source,
            dataset_name=f"dataset-{number}",
            schema_plan=confirm_plan(analyze_csv(source), {0: "label"}, custom_types=(custom,)),
        )
    monkeypatch.setattr("pandoracle.universal_search._SCAN_PERMISSION_SECONDS", 0.04)

    with pytest.raises(SearchFailure, match="estimated.*rerun with --scan"):
        execute_search(workspace, SearchRequest((SearchClue("label", "alpha"),)))


def test_unscoped_search_reports_an_unresolvable_active_dataset(tmp_path: Path) -> None:
    workspace, imported = _import_people(tmp_path)
    with workspace.catalog.transaction() as connection:
        connection.execute(
            "UPDATE datasets SET active_canonical_profile_id=? WHERE id=?",
            ("00000000-0000-0000-0000-000000000000", imported.dataset_id),
        )

    assert workspace.catalog.list_sources()[0]["active_version_id"] == imported.dataset_version_id
    with pytest.raises(
        SearchFailure,
        match="cannot resolve active dataset 'people': active canonical profile is missing",
    ):
        execute_search(workspace, SearchRequest((SearchClue("EMAIL", "anna@example.test"),)))
