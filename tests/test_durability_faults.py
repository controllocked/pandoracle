from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pandoracle.acceleration_profiles import (
    configure_acceleration,
    confirm_acceleration_plan,
    make_acceleration_plan,
)
from pandoracle.ingest import analyze_csv, import_csv
from pandoracle.maintenance import apply_gc, plan_gc, recover, verify_workspace
from pandoracle.models import AccelerationKind
from pandoracle.revision import analyze_dataset, revise_dataset
from pandoracle.schema import confirm_plan
from pandoracle.workspace import Workspace

IMPORT_POINTS = (
    "import.operation_cataloged",
    "import.raw_staged",
    "import.raw_published",
    "import.source_cataloged",
    "import.version_cataloged",
    "import.artifacts_staged",
    "import.artifacts_validated",
    "import.record_finalized",
    "import.canonical_finalized",
    "import.before_catalog_commit",
    "import.after_catalog_commit",
)
SCHEMA_REVISION_POINTS = (
    "schema_revision.version_cataloged",
    "schema_revision.artifacts_staged",
    "schema_revision.artifacts_validated",
    "schema_revision.record_finalized",
    "schema_revision.canonical_finalized",
    "schema_revision.before_catalog_commit",
    "schema_revision.after_catalog_commit",
)
ACCELERATION_POINTS = (
    "acceleration.profile_cataloged",
    "acceleration.artifacts_validated",
    "acceleration.artifacts_finalized",
    "acceleration.before_catalog_commit",
    "acceleration.after_catalog_commit",
)
GC_POINTS = (
    "gc.operation_cataloged",
    "gc.after_filesystem_move",
    "gc.after_filesystem_moves",
    "gc.before_catalog_commit",
    "gc.after_catalog_commit",
    "gc.after_trash_delete",
)


def _run_crashing_process(code: str, *arguments: Path, point: str) -> None:
    environment = os.environ.copy()
    environment["PANDORACLE_TEST_FAULT_POINT"] = f"{point}:exit"
    result = subprocess.run(
        [sys.executable, "-c", code, *(str(item) for item in arguments)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 97, result.stderr


def _published_count(workspace: Workspace, table: str) -> int:
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        return int(
            connection.execute(f"SELECT count(*) FROM {table} WHERE status='PUBLISHED'").fetchone()[
                0
            ]
        )


@pytest.mark.parametrize("point", IMPORT_POINTS)
def test_import_crash_matrix_recovers_cleans_and_retries(tmp_path: Path, point: str) -> None:
    first_source = tmp_path / "first.csv"
    first_source.write_text("email\nfirst@example.test\n", encoding="utf-8")
    second_source = tmp_path / "second.csv"
    second_source.write_text("email\nsecond@example.test\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    first = import_csv(
        workspace,
        first_source,
        dataset_name="people",
        schema_plan=confirm_plan(analyze_csv(first_source)),
    )
    code = """
import sys
from pathlib import Path
from pandoracle.workspace import Workspace
from pandoracle.ingest import analyze_csv, import_csv
from pandoracle.schema import confirm_plan
w = Workspace.open(Path(sys.argv[1]))
s = Path(sys.argv[2])
import_csv(w, s, dataset_name='people', schema_plan=confirm_plan(analyze_csv(s)))
"""
    _run_crashing_process(code, workspace.root, second_source, point=point)

    dataset = workspace.catalog.find_dataset("people")
    assert dataset is not None
    committed = point == "import.after_catalog_commit"
    if not committed:
        assert dataset["active_version_id"] == first.dataset_version_id
    recover(workspace)
    apply_gc(workspace)
    retried = import_csv(
        workspace,
        second_source,
        dataset_name="people",
        schema_plan=confirm_plan(analyze_csv(second_source)),
    )

    assert retried.dataset_version_id != first.dataset_version_id
    assert _published_count(workspace, "dataset_versions") == 2
    assert plan_gc(workspace).items == ()
    verify_workspace(workspace)


@pytest.mark.parametrize("point", SCHEMA_REVISION_POINTS)
def test_schema_revision_crash_matrix_preserves_old_ref_and_retries(
    tmp_path: Path, point: str
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
    code = """
import sys
from pathlib import Path
from pandoracle.workspace import Workspace
from pandoracle.revision import analyze_dataset, revise_dataset
from pandoracle.schema import confirm_plan
w = Workspace.open(Path(sys.argv[1]))
p = confirm_plan(analyze_dataset(w, 'people'), {0: 'PERSON_NAME'})
revise_dataset(w, 'people', p)
"""
    _run_crashing_process(code, workspace.root, point=point)

    dataset = workspace.catalog.find_dataset("people")
    assert dataset is not None
    committed = point == "schema_revision.after_catalog_commit"
    if not committed:
        assert dataset["active_version_id"] == first.dataset_version_id
    recover(workspace)
    apply_gc(workspace)
    revised = revise_dataset(
        workspace,
        "people",
        confirm_plan(analyze_dataset(workspace, "people"), {0: "PERSON_NAME"}),
    )

    assert revised.dataset_version_id != first.dataset_version_id
    assert _published_count(workspace, "dataset_versions") == 2
    assert plan_gc(workspace).items == ()
    verify_workspace(workspace)


@pytest.mark.parametrize("point", ACCELERATION_POINTS)
def test_acceleration_crash_matrix_preserves_active_profile_and_retries(
    tmp_path: Path, point: str
) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    imported = import_csv(
        workspace,
        source,
        dataset_name="people",
        schema_plan=confirm_plan(analyze_csv(source)),
    )
    code = """
import sys
from pathlib import Path
from pandoracle.workspace import Workspace
from pandoracle.acceleration_profiles import (
    configure_acceleration,
    confirm_acceleration_plan,
    make_acceleration_plan,
)
from pandoracle.models import AccelerationKind
w = Workspace.open(Path(sys.argv[1]))
p = make_acceleration_plan(w, 'people')
p = confirm_acceleration_plan(p, {0: (AccelerationKind.VALUE,)})
configure_acceleration(w, 'people', p)
"""
    _run_crashing_process(code, workspace.root, point=point)

    dataset = workspace.catalog.find_dataset("people")
    assert dataset is not None
    committed = point == "acceleration.after_catalog_commit"
    if not committed:
        assert dataset["active_index_profile_id"] == imported.index_profile_id
    recover(workspace)
    apply_gc(workspace)
    plan = make_acceleration_plan(workspace, "people")
    revised = configure_acceleration(
        workspace,
        "people",
        confirm_acceleration_plan(plan, {0: (AccelerationKind.VALUE,)}),
    )

    assert revised.dataset_version_id == imported.dataset_version_id
    assert _published_count(workspace, "index_profiles") == 1
    apply_gc(workspace)
    assert plan_gc(workspace).items == ()
    verify_workspace(workspace)


@pytest.mark.parametrize(
    ("workflow", "point"),
    [
        ("schema", "schema_revision.canonical_finalized"),
        ("acceleration", "acceleration.artifacts_finalized"),
    ],
)
def test_ctrl_c_recovery_for_revision_writers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow: str,
    point: str,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text("full_name\nAlex Smith\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    import_csv(
        workspace,
        source,
        dataset_name="people",
        schema_plan=confirm_plan(
            analyze_csv(source),
            {0: "UNKNOWN" if workflow == "schema" else "PERSON_NAME"},
        ),
    )
    monkeypatch.setenv("PANDORACLE_TEST_FAULT_POINT", f"{point}:interrupt")

    with pytest.raises(KeyboardInterrupt):
        if workflow == "schema":
            revise_dataset(
                workspace,
                "people",
                confirm_plan(analyze_dataset(workspace, "people"), {0: "PERSON_NAME"}),
            )
        else:
            plan = make_acceleration_plan(workspace, "people")
            configure_acceleration(
                workspace,
                "people",
                confirm_acceleration_plan(plan, {0: (AccelerationKind.TOKEN,)}),
            )

    recover(workspace)
    apply_gc(workspace)
    assert plan_gc(workspace).items == ()
    verify_workspace(workspace)


@pytest.mark.parametrize("point", GC_POINTS)
def test_gc_crash_matrix_is_recoverable_and_idempotent(tmp_path: Path, point: str) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    orphan = workspace.root / "operations/orphans/abandoned"
    orphan.mkdir()
    (orphan / "data").write_bytes(b"discard")
    code = """
import sys
from pathlib import Path
from pandoracle.workspace import Workspace
from pandoracle.maintenance import apply_gc
apply_gc(Workspace.open(Path(sys.argv[1])))
"""
    _run_crashing_process(code, workspace.root, point=point)

    recover(workspace)
    apply_gc(workspace)

    assert plan_gc(workspace).items == ()
    assert apply_gc(workspace).operation_id is None
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM operations WHERE kind='GC' AND status NOT IN "
                "('PUBLISHED', 'FAILED', 'ABORTED')"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "point",
    (
        "recover.before_catalog_commit",
        "recover.after_catalog_commit",
        "recover.after_quarantine",
    ),
)
def test_recovery_crash_matrix_is_idempotent(tmp_path: Path, point: str) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    operation_id = "interrupted-operation"
    stage = workspace.root / "operations/staging" / operation_id
    stage.mkdir()
    (stage / "data").write_bytes(b"discard")
    with workspace.catalog.transaction() as connection:
        connection.execute(
            """
            INSERT INTO operations(id, kind, status, started_at, updated_at)
            VALUES (?, 'IMPORT', 'TRANSFORMING', '2026-01-01', '2026-01-01')
            """,
            (operation_id,),
        )
    code = """
import sys
from pathlib import Path
from pandoracle.workspace import Workspace
from pandoracle.maintenance import recover
recover(Workspace.open(Path(sys.argv[1])))
"""
    _run_crashing_process(code, workspace.root, point=point)

    recover(workspace)
    assert recover(workspace) == []
    apply_gc(workspace)
    assert plan_gc(workspace).items == ()
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        assert (
            connection.execute(
                "SELECT status FROM operations WHERE id=?", (operation_id,)
            ).fetchone()[0]
            == "ABORTED"
        )
