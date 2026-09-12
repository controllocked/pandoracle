import contextlib
import uuid
from pathlib import Path

from pandoracle.fs import utc_now
from pandoracle.maintenance import recover
from pandoracle.workspace import Workspace


def test_recover_aborts_and_quarantines_staging(tmp_path: Path) -> None:
    workspace = Workspace.create(tmp_path / "workspace")
    operation_id = str(uuid.uuid4())
    staging = workspace.root / "operations/staging" / operation_id
    staging.mkdir()
    (staging / "partial").write_text("not published", encoding="utf-8")
    now = utc_now()
    with workspace.catalog.transaction() as connection:
        connection.execute(
            """
            INSERT INTO operations(id, kind, status, started_at, updated_at)
            VALUES (?, 'IMPORT', 'TRANSFORMING', ?, ?)
            """,
            (operation_id, now, now),
        )

    recovered = recover(workspace)
    assert recovered[0]["operation_id"] == operation_id
    orphan = Path(recovered[0]["quarantined_path"])
    assert (orphan / "partial").read_text(encoding="utf-8") == "not published"
    with contextlib.closing(workspace.catalog.connect(read_only=True)) as connection:
        status = connection.execute(
            "SELECT status FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()[0]
    assert status == "ABORTED"
