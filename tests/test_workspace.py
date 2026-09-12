import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from pandoracle.errors import WorkspaceError
from pandoracle.ingest import analyze_csv, import_csv
from pandoracle.schema import confirm_plan
from pandoracle.workspace import Workspace


def test_workspace_init_and_reopen(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    created = Workspace.create(root)
    reopened = Workspace.open(root)
    assert created.manifest.workspace_id == reopened.manifest.workspace_id
    assert (root / "catalog.sqlite").is_file()
    assert (root / "objects/raw/sha256").is_dir()
    assert not (root / "indexes").exists()
    with contextlib.closing(reopened.catalog.connect(read_only=True)) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='catalog_schema_version'"
        ).fetchone()[0] == "5"
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(custom_semantic_types)")
        }
        assert "retired_at" in columns
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='index_policy_preferences'"
        ).fetchone() is None


def test_workspace_rejects_nonempty_target(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "keep.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="must be empty"):
        Workspace.create(root)
    assert (root / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_workspace_rejects_identity_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    Workspace.create(root)
    manifest_path = root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["workspace_id"] = "0a502684-f1ca-4db5-b3ca-bfde307876e2"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="identities differ"):
        Workspace.open(root)


def test_workspace_rejects_invalid_catalog_version_as_a_domain_error(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    Workspace.create(root)
    with contextlib.closing(sqlite3.connect(root / "catalog.sqlite")) as connection:
        connection.execute(
            "UPDATE metadata SET value='not-a-version' WHERE key='catalog_schema_version'"
        )
        connection.commit()

    with pytest.raises(WorkspaceError, match="invalid schema version"):
        Workspace.open(root)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_pre_v4_catalogs_are_rejected_with_recreation_guidance(
    tmp_path: Path, version: int
) -> None:
    root = tmp_path / f"workspace-{version}"
    Workspace.create(root)
    with contextlib.closing(sqlite3.connect(root / "catalog.sqlite")) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='catalog_schema_version'",
            (str(version),),
        )
        connection.commit()

    with pytest.raises(WorkspaceError, match=rf"schema {version}.*new v1 workspace"):
        Workspace.open(root)
    assert not list((root / "operations/catalog-backups").iterdir())


def test_catalog_v4_is_backed_up_and_migrated_once(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = Workspace.create(root)
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    imported = import_csv(
        workspace,
        source,
        dataset_name="people",
        schema_plan=confirm_plan(analyze_csv(source)),
    )
    before = workspace.catalog.find_dataset("people")
    assert before is not None

    with contextlib.closing(sqlite3.connect(root / "catalog.sqlite")) as connection:
        fresh_policy = connection.execute(
            "SELECT policy_json,policy_version FROM index_profiles"
        ).fetchone()
        assert fresh_policy[1] == 1
        assert json.loads(fresh_policy[0])["policy_version"] == 1
        connection.execute("ALTER TABLE custom_semantic_types DROP COLUMN retired_at")
        profile = connection.execute(
            "SELECT id, policy_json, build_metrics_json FROM index_profiles"
        ).fetchone()
        policy = json.loads(profile[1])
        policy["policy_version"] = 2
        metrics = json.loads(profile[2])
        if metrics:
            metrics["metrics_version"] = 2
        connection.execute(
            "UPDATE index_profiles SET policy_json=?,policy_version=2,build_metrics_json=? "
            "WHERE id=?",
            (json.dumps(policy), json.dumps(metrics), profile[0]),
        )
        connection.execute("UPDATE metadata SET value='4' WHERE key='catalog_schema_version'")
        connection.commit()

    migrated = Workspace.open(root)
    reopened = Workspace.open(root)
    after = reopened.catalog.find_dataset("people")
    assert after is not None
    assert after["active_version_id"] == before["active_version_id"]
    assert after["active_canonical_profile_id"] == imported.canonical_profile_id
    assert after["active_index_profile_id"] == imported.index_profile_id

    backups = list((root / "operations/catalog-backups").glob("catalog-v4-*.sqlite"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert migrated.manifest.workspace_id == workspace.manifest.workspace_id
    with contextlib.closing(migrated.catalog.connect(read_only=True)) as connection:
        row = connection.execute(
            "SELECT policy_json,policy_version,build_metrics_json FROM index_profiles"
        ).fetchone()
        assert row[1] == 1
        assert json.loads(row[0])["policy_version"] == 1
        stored_metrics = json.loads(row[2])
        if stored_metrics:
            assert stored_metrics["metrics_version"] == 1


def test_workspace_with_pre_v1_exact_artifact_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = Workspace.create(root)
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    import_csv(
        workspace,
        source,
        schema_plan=confirm_plan(analyze_csv(source)),
    )
    with workspace.catalog.transaction() as connection:
        connection.execute(
            "UPDATE artifacts SET kind='EXACT_INDEX' WHERE kind='RECORD_PARQUET'"
        )

    with pytest.raises(WorkspaceError, match="EXACT acceleration.*new v1 workspace"):
        Workspace.open(root)
