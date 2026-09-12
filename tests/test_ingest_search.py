import contextlib
import csv
import json
from pathlib import Path

import pytest

from pandoracle.errors import ImportFailure, SearchFailure, WorkspaceError
from pandoracle.ingest import MalformedRecordAction, analyze_csv, import_csv
from pandoracle.maintenance import verify_workspace
from pandoracle.models import SearchClue, SearchRequest, SemanticType
from pandoracle.schema import confirm_plan
from pandoracle.search import inspect_record, search
from pandoracle.workspace import Workspace


def write_people(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(("full_name", "email", "telephone", "city"))
        writer.writerow(("Alexey Ivanov", "John@Example.COM", "+38 (050) 123-45-67", "Kyiv"))
        writer.writerow(("Maria Smith", "maria@example.test", "+48 500 100 200", "Warsaw"))
        writer.writerow(("John Duplicate", "John@Example.COM", "+44 20 1234 5678", "London"))


def confirmed_schema(path: Path):
    return confirm_plan(analyze_csv(path))


def test_vertical_slice_and_provenance(tmp_path: Path) -> None:
    source = tmp_path / "people.csv"
    write_people(source)
    workspace = Workspace.create(tmp_path / "workspace")

    imported = import_csv(
        workspace, source, dataset_name="people", schema_plan=confirmed_schema(source)
    )
    assert imported.row_count == 3
    assert imported.posting_count == 0
    assert not imported.deduplicated

    result = search(
        workspace,
        SearchRequest((SearchClue(SemanticType.EMAIL, "john@example.com"),)),
    )
    assert len(result.records) == 2
    provenances = [item.matches[0].provenances[0] for item in result.records]
    assert {item.record_ordinal for item in provenances} == {0, 2}
    assert all(item.source_sha256 == imported.source_sha256 for item in provenances)
    assert all(item.original_value == "John@Example.COM" for item in provenances)

    record = inspect_record(workspace, result.records[0].ref.external_id)
    assert record["record"]["full_name"] == "Alexey Ivanov"
    assert record["source_sha256"] == imported.source_sha256

    raw = workspace.root / "objects/raw/sha256" / imported.source_sha256
    assert raw.read_bytes() == source.read_bytes()
    assert (
        workspace.root
        / "datasets"
        / imported.dataset_id
        / imported.dataset_version_id
        / "data/part-00000.parquet"
    ).is_file()
    verified = verify_workspace(workspace)
    assert verified["raw_blobs"] == 1
    assert verified["artifacts"] == 2


def test_new_unknown_field_does_not_trigger_implicit_legacy_scan(tmp_path: Path) -> None:
    source = tmp_path / "people.csv"
    source.write_text(
        "ФИО,Дата рождения,ИНН,Телефон\n"
        "Ли Вадим Владимирович,1990-02-14,123456789012,+77001234567\n"
        "Иванова Анна Сергеевна,1988-06-10,980610123456,+77007654321\n",
        encoding="utf-8",
    )
    workspace = Workspace.create(tmp_path / "workspace")
    imported = import_csv(
        workspace,
        source,
        dataset_name="Kazakhstan",
        schema_plan=confirm_plan(
            analyze_csv(source),
            {0: "UNKNOWN", 1: "DATE", 2: "UNKNOWN", 3: "PHONE"},
        ),
    )

    assert imported.fields[0]["semantic_type"] == "UNKNOWN"
    assert imported.fields[1]["semantic_type"] == "DATE"
    assert imported.fields[2]["semantic_type"] == "UNKNOWN"
    assert imported.fields[3]["semantic_type"] == "PHONE"
    result = search(
        workspace,
        SearchRequest((SearchClue("PERSON_NAME", "Ли Вадим Владимирович"),), dataset="Kazakhstan"),
    )
    assert result.records == ()
    with pytest.raises(SearchFailure, match="UNKNOWN is not searchable"):
        search(workspace, SearchRequest((SearchClue("UNKNOWN", "Ли"),)))


def test_duplicate_import_reuses_published_version(tmp_path: Path) -> None:
    source = tmp_path / "people.csv"
    write_people(source)
    workspace = Workspace.create(tmp_path / "workspace")
    plan = confirmed_schema(source)
    first = import_csv(workspace, source, dataset_name="people", schema_plan=plan)
    second = import_csv(workspace, source, dataset_name="people", schema_plan=plan)
    assert second.deduplicated
    assert second.dataset_version_id == first.dataset_version_id
    assert second.row_count == first.row_count
    assert len(workspace.catalog.list_sources()) == 1


def test_failed_reimport_does_not_replace_active_version(tmp_path: Path) -> None:
    source = tmp_path / "people.csv"
    write_people(source)
    workspace = Workspace.create(tmp_path / "workspace")
    first = import_csv(
        workspace, source, dataset_name="people", schema_plan=confirmed_schema(source)
    )

    broken = tmp_path / "broken.csv"
    valid_rows = "".join(
        f"user{ordinal}@example.test,+3805000{ordinal:04d}\n" for ordinal in range(513)
    )
    broken.write_text("email,phone\n" + valid_rows + "only-one-field\n", encoding="utf-8")
    with pytest.raises(ImportFailure):
        import_csv(
            workspace,
            broken,
            dataset_name="people",
            schema_plan=confirm_plan(analyze_csv(broken)),
        )

    dataset = workspace.catalog.find_dataset("people")
    assert dataset is not None
    assert dataset["active_version_id"] == first.dataset_version_id
    assert (
        len(
            search(
                workspace,
                SearchRequest((SearchClue(SemanticType.EMAIL, "john@example.com"),)),
            ).records
        )
        == 2
    )
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        statuses = [
            row[0]
            for row in connection.execute("SELECT status FROM dataset_versions ORDER BY created_at")
        ]
    assert statuses == ["PUBLISHED", "FAILED"]


def test_quarantine_preserves_malformed_records_and_contiguous_record_refs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "malformed.csv"
    source.write_text(
        "email,city\n"
        "first@example.test,Almaty\n"
        "missing-city\n"
        'broken@example.test,"Astana"oops\n'
        "last@example.test,Karaganda\n",
        encoding="utf-8",
    )
    workspace = Workspace.create(tmp_path / "workspace")

    imported = import_csv(
        workspace,
        source,
        schema_plan=confirm_plan(analyze_csv(source)),
        on_error="quarantine",
    )

    assert imported.row_count == 2
    assert imported.quarantined_count == 2
    assert imported.quarantine_path is not None
    rejected = [
        json.loads(line)
        for line in (workspace.root / imported.quarantine_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rejected == [
        {
            "actual_fields": 1,
            "expected_fields": 2,
            "line_end": 3,
            "line_start": 3,
            "raw_record": "missing-city\n",
            "reason": "field count mismatch",
            "source_record_number": 2,
        },
        {
            "actual_fields": None,
            "expected_fields": 2,
            "line_end": 4,
            "line_start": 4,
            "raw_record": 'broken@example.test,"Astana"oops\n',
            "reason": "CSV parse error: ',' expected after '\"'",
            "source_record_number": 3,
        },
    ]
    assert inspect_record(
        workspace, f"{imported.dataset_version_id}:1"
    )["record"] == {"email": "last@example.test", "city": "Karaganda"}
    assert verify_workspace(workspace)["artifacts"] == 3


def test_quarantine_all_similar_only_calls_handler_once(tmp_path: Path) -> None:
    source = tmp_path / "malformed.csv"
    source.write_text("a,b\none\nvalid,row\ntwo\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    prompted = []

    imported = import_csv(
        workspace,
        source,
        schema_plan=confirm_plan(analyze_csv(source)),
        malformed_handler=lambda record: (
            prompted.append(record) or MalformedRecordAction.QUARANTINE_SIMILAR
        ),
    )

    assert imported.row_count == 1
    assert imported.quarantined_count == 2
    assert len(prompted) == 1


def test_quarantine_safety_limit_aborts_high_reject_rate(tmp_path: Path) -> None:
    source = tmp_path / "mostly-malformed.csv"
    source.write_text("a,b\n" + "bad\n" * 100, encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")

    with pytest.raises(ImportFailure, match="safety limit reached.*100 of 100"):
        import_csv(
            workspace,
            source,
            schema_plan=confirm_plan(analyze_csv(source)),
            on_error="quarantine",
        )

    assert workspace.catalog.find_dataset("mostly-malformed")["active_version_id"] is None


def test_explicit_schema_override(tmp_path: Path) -> None:
    source = tmp_path / "custom.csv"
    source.write_text("contact_value\nUser@Example.COM\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    imported = import_csv(
        workspace,
        source,
        dataset_name="custom",
        schema_plan=confirm_plan(analyze_csv(source), {0: "EMAIL"}),
    )
    assert imported.fields[0]["semantic_type"] == "EMAIL"
    result = search(
        workspace,
        SearchRequest((SearchClue(SemanticType.EMAIL, "user@example.com"),)),
    )
    assert len(result.records) == 1


def test_verification_detects_raw_tampering(tmp_path: Path) -> None:
    source = tmp_path / "people.csv"
    write_people(source)
    workspace = Workspace.create(tmp_path / "workspace")
    imported = import_csv(
        workspace, source, dataset_name="people", schema_plan=confirmed_schema(source)
    )
    raw = workspace.root / "objects/raw/sha256" / imported.source_sha256
    with raw.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(WorkspaceError, match="RAW blob verification failed"):
        verify_workspace(workspace)
