from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_xdg_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep tests independent from an active desktop device session."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
