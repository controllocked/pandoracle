from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pandoracle import cli
from pandoracle.errors import WorkspaceError
from pandoracle.ingest import ImportProgress, analyze_csv, import_csv
from pandoracle.maintenance import apply_gc, plan_gc, recover, verify_workspace
from pandoracle.models import OperationStatus
from pandoracle.revision import analyze_dataset, revise_dataset
from pandoracle.schema import confirm_plan
from pandoracle.search import inspect_record
from pandoracle.workspace import Workspace


def _import(workspace: Workspace, source: Path, *, dataset: str = "people"):
    return import_csv(
        workspace,
        source,
        dataset_name=dataset,
        schema_plan=confirm_plan(analyze_csv(source)),
    )


def test_recover_gc_and_retry_interrupted_import(tmp_path: Path) -> None:
    first_source = tmp_path / "first.csv"
    first_source.write_text("email\nfirst@example.test\n", encoding="utf-8")
    second_source = tmp_path / "second.csv"
    second_source.write_text("email\nsecond@example.test\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    first = _import(workspace, first_source)

    def interrupt(progress: ImportProgress) -> None:
        if progress.phase is OperationStatus.TRANSFORMING:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        import_csv(
            workspace,
            second_source,
            dataset_name="people",
            schema_plan=confirm_plan(analyze_csv(second_source)),
            progress=interrupt,
        )

    blocked = plan_gc(workspace)
    assert any("run recover first" in blocker for blocker in blocked.blockers)
    with pytest.raises(WorkspaceError, match="run recover first"):
        apply_gc(workspace)
    assert workspace.catalog.find_dataset("people")["active_version_id"] == (
        first.dataset_version_id
    )
    assert recover(workspace)
    assert recover(workspace) == []

    plan = plan_gc(workspace)
    assert plan_gc(workspace) == plan
    assert plan.blockers == ()
    assert {item.kind for item in plan.items} >= {
        "dataset_version",
        "quarantine",
        "raw",
        "source_blob",
    }
    result = apply_gc(workspace)
    assert result.operation_id is not None
    assert plan_gc(workspace).items == ()
    assert verify_workspace(workspace)["artifacts"] == 2
    assert inspect_record(workspace, f"{first.dataset_version_id}:0")["record"]["email"] == (
        "first@example.test"
    )

    retried = _import(workspace, second_source)
    assert retried.dataset_version_id != first.dataset_version_id
    assert verify_workspace(workspace)["artifacts"] == 4
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        statuses = list(
            connection.execute("SELECT kind, status FROM operations ORDER BY started_at, id")
        )
    assert ("IMPORT", "ABORTED") in {(row[0], row[1]) for row in statuses}
    assert ("GC", "PUBLISHED") in {(row[0], row[1]) for row in statuses}


def test_gc_discovers_catalog_invisible_raw_and_final_artifacts(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    raw = workspace.root / "objects/raw/sha256" / ("a" * 64)
    raw.write_bytes(b"abandoned raw")
    final_paths = (
        workspace.root / "datasets/dataset-id/version-id/data/part-00000.parquet",
        workspace.root / "canonical/v1/profile-id/part-00000.parquet",
        workspace.root / "accelerations/v1/index-id/value.sqlite",
    )
    for path in final_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"abandoned final")

    before = sorted(path.relative_to(workspace.root) for path in workspace.root.rglob("*"))
    plan = plan_gc(workspace)
    after = sorted(path.relative_to(workspace.root) for path in workspace.root.rglob("*"))

    assert before == after
    assert plan.blockers == ()
    assert sum(item.kind == "final_artifact" for item in plan.items) == 3
    assert sum(item.kind == "raw" for item in plan.items) == 1

    apply_gc(workspace)
    assert not raw.exists()
    assert all(not path.exists() for path in final_paths)
    assert plan_gc(workspace).items == ()


def test_gc_preserves_all_published_versions_and_shared_raw(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("full_name\nAlex Smith\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    first = import_csv(
        workspace,
        source,
        dataset_name="people",
        schema_plan=confirm_plan(analyze_csv(source), {0: "UNKNOWN"}),
    )
    second = revise_dataset(
        workspace,
        "people",
        confirm_plan(analyze_dataset(workspace, "people"), {0: "PERSON_NAME"}),
    )
    published_paths: set[Path] = set()
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM dataset_versions WHERE status='PUBLISHED'"
            ).fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT count(*) FROM source_blobs").fetchone()[0] == 1
        for row in connection.execute("SELECT relative_path FROM artifacts"):
            published_paths.add(workspace.root / row[0])
        for row in connection.execute("SELECT stored_path FROM source_blobs"):
            published_paths.add(workspace.root / row[0])

    assert plan_gc(workspace).items == ()
    assert apply_gc(workspace).operation_id is None
    assert all(path.exists() for path in published_paths)
    assert inspect_record(workspace, f"{first.dataset_version_id}:0")["record"]["full_name"] == (
        "Alex Smith"
    )
    assert inspect_record(workspace, f"{second.dataset_version_id}:0")["record"]["full_name"] == (
        "Alex Smith"
    )


def test_gc_refuses_symlinks_and_missing_published_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    _import(workspace, source)
    unsafe = workspace.root / "operations/orphans/unsafe"
    unsafe.symlink_to(source)

    plan = plan_gc(workspace)
    assert any("symlink" in blocker for blocker in plan.blockers)
    with pytest.raises(WorkspaceError, match="blocked"):
        apply_gc(workspace)

    unsafe.unlink()
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        artifact = connection.execute(
            "SELECT relative_path FROM artifacts ORDER BY relative_path LIMIT 1"
        ).fetchone()[0]
    (workspace.root / artifact).unlink()
    plan = plan_gc(workspace)
    assert any("published artifact is missing" in blocker for blocker in plan.blockers)


def test_gc_cli_json_is_versioned_and_apply_is_explicit(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    orphan = workspace.root / "operations/orphans/old"
    orphan.mkdir()
    (orphan / "content").write_text("discard", encoding="utf-8")
    runner = CliRunner()

    dry_run = runner.invoke(
        cli.app,
        ["maintenance", "gc", "--workspace", str(workspace.root), "--format", "json"],
    )
    assert dry_run.exit_code == 0
    dry_value = json.loads(dry_run.stdout)
    assert dry_value["gc_version"] == 1
    assert dry_value["mode"] == "dry-run"
    assert orphan.exists()

    applied = runner.invoke(
        cli.app,
        [
            "maintenance",
            "gc",
            "--workspace",
            str(workspace.root),
            "--apply",
            "--format",
            "json",
        ],
    )
    assert applied.exit_code == 0
    applied_value = json.loads(applied.stdout)
    assert applied_value["mode"] == "apply"
    assert applied_value["operation_id"]
    assert not orphan.exists()
