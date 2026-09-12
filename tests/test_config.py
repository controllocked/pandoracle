import json
import os
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text
from typer.testing import CliRunner

from pandoracle import cli
from pandoracle.config import (
    active_device_path,
    active_device_workspace,
    config_path,
    default_workspace_path,
    select_workspace,
    selected_workspace,
    set_active_device,
)
from pandoracle.errors import WorkspaceError
from pandoracle.workspace import Workspace


def test_xdg_defaults_and_atomic_selection_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    assert config_path() == config_home / "pandoracle/config.json"
    assert default_workspace_path() == data_home / "pandoracle/workspace"
    assert selected_workspace() is None

    workspace = Workspace.create(tmp_path / "workspace")
    assert select_workspace(workspace.root) == workspace.root.resolve()
    assert selected_workspace() == workspace.root.resolve()
    assert config_path().stat().st_mode & 0o777 == 0o600
    assert config_path().parent.stat().st_mode & 0o777 == 0o700
    assert not config_path().with_name(".config.json.tmp").exists()


def test_workspace_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    saved = Workspace.create(tmp_path / "saved")
    env_workspace = Workspace.create(tmp_path / "environment")
    explicit = Workspace.create(tmp_path / "explicit")
    select_workspace(saved.root)

    monkeypatch.setenv("PANDORACLE_WORKSPACE", str(env_workspace.root))
    assert cli._workspace_path(None) == env_workspace.root
    assert cli._workspace_path(explicit.root) == explicit.root
    monkeypatch.delenv("PANDORACLE_WORKSPACE")
    assert cli._workspace_path(None) == saved.root.resolve()


def test_active_device_is_used_without_changing_saved_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    saved = Workspace.create(tmp_path / "saved")
    device = Workspace.create(tmp_path / "device")
    select_workspace(saved.root)

    set_active_device(device_id="device", workspace=device.root, pid=os.getpid())

    assert active_device_workspace() == device.root
    assert cli._workspace_path(None) == device.root
    assert selected_workspace() == saved.root.resolve()


def test_stale_active_device_runtime_state_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    workspace = Workspace.create(tmp_path / "device")
    path = active_device_path()
    path.write_text(
        json.dumps(
            {
                "active_device_version": 1,
                "device_id": "stale",
                "pid": 2_000_000_000,
                "workspace": str(workspace.root),
            }
        ),
        encoding="utf-8",
    )

    assert active_device_workspace() is None
    assert not path.exists()


def test_missing_and_corrupt_config_are_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert selected_workspace() is None
    config_path().parent.mkdir(parents=True)
    config_path().write_text("{broken", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="invalid user config.*pandoracle workspace PATH"):
        selected_workspace()


def test_init_and_workspace_commands_persist_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    runner = CliRunner()

    initialized = runner.invoke(cli.app, ["init", "--format", "json"])
    assert initialized.exit_code == 0
    value = json.loads(initialized.stdout)
    assert value["workspace"] == str(default_workspace_path())
    assert value["selected"] is True

    another = Workspace.create(tmp_path / "another")
    chosen = runner.invoke(cli.app, ["workspace", str(another.root), "--format", "json"])
    assert chosen.exit_code == 0
    assert selected_workspace() == another.root.resolve()
    shown = runner.invoke(cli.app, ["workspace", "--format", "json"])
    assert json.loads(shown.stdout)["workspace"] == str(another.root.resolve())


def test_unavailable_selected_workspace_has_a_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config_path().parent.mkdir(parents=True)
    config_path().write_text(
        json.dumps({"config_version": 1, "workspace": str(tmp_path / "gone")}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli.app, ["datasets"])
    assert result.exit_code != 0
    assert "workspace path is unavailable" in str(result.exception)


def test_branding_widths_and_non_tty_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    original = cli.console
    try:
        wide = StringIO()
        cli.console = Console(file=wide, width=80, color_system=None, force_terminal=False)
        cli._emit_brand(width=80)
        wide_lines = wide.getvalue().splitlines()
        assert "%%-  :%%%%%%%%%%%%%%%%%-  :%%" in wide.getvalue()
        assert "_ __" in wide.getvalue()
        assert "PANDORACLE" not in wide.getvalue()
        assert max(map(len, wide_lines)) <= 80
        assert wide.getvalue().endswith("hunt through private data\n\n")

        medium = StringIO()
        cli.console = Console(file=medium, width=42, color_system=None, force_terminal=False)
        cli._emit_brand(width=42)
        medium_lines = medium.getvalue().splitlines()
        assert "%*  %%%%%%%%%%%%%%%%%  +%" in medium.getvalue()
        assert "PANDORACLE" in medium.getvalue()
        assert max(map(len, medium_lines)) <= 42

        narrow = StringIO()
        cli.console = Console(file=narrow, width=30, color_system=None, force_terminal=False)
        cli._emit_brand(width=30)
        assert "PANDORACLE" in narrow.getvalue()
        assert "____" not in narrow.getvalue()
    finally:
        cli.console = original

    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 0
    plain_output = Text.from_ansi(result.stdout).plain
    assert "Usage: pandoracle" in plain_output
    assert "hunt through private data" not in plain_output
