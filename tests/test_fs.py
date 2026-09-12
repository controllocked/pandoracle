import json
from pathlib import Path

import pytest

from pandoracle import fs


def test_atomic_json_write_ignores_a_stale_legacy_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    stale = target.with_name(".config.json.tmp")
    stale.write_text("interrupted old write", encoding="utf-8")

    fs.write_json_atomic(target, {"version": 1})

    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1}
    assert stale.read_text(encoding="utf-8") == "interrupted old write"
    assert list(tmp_path.glob(".config.json.*.tmp")) == []


def test_atomic_json_write_removes_its_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.json"

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(fs.os, "replace", fail_replace)

    with pytest.raises(OSError, match="synthetic replace failure"):
        fs.write_json_atomic(target, {"version": 1})

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
