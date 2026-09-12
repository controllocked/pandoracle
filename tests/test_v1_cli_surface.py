import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pandoracle import cli
from pandoracle.acceleration_profiles import (
    confirm_acceleration_plan,
    load_acceleration_plan,
)
from pandoracle.config import select_workspace
from pandoracle.errors import ImportFailure
from pandoracle.fs import write_json_atomic
from pandoracle.ingest import analyze_csv, import_csv
from pandoracle.schema import confirm_plan, load_schema_document
from pandoracle.workspace import Workspace

PUBLIC_HELP_PATHS = (
    (),
    ("init",),
    ("workspace",),
    ("import",),
    ("datasets",),
    ("inspect",),
    ("search",),
    ("shell",),
    ("types",),
    ("types", "retire"),
    ("types", "restore"),
    ("schema",),
    ("schema", "analyze"),
    ("schema", "review"),
    ("schema", "revise"),
    ("acceleration",),
    ("acceleration", "show"),
    ("acceleration", "plan"),
    ("acceleration", "configure"),
    ("acceleration", "rebuild"),
    ("acceleration", "disable"),
    ("maintenance",),
    ("maintenance", "verify"),
    ("maintenance", "recover"),
    ("maintenance", "gc"),
    ("maintenance", "operations"),
    ("device",),
    ("device", "setup"),
    ("device", "trust"),
    ("device", "list"),
    ("device", "open"),
    ("device", "close"),
    ("device", "forget"),
    ("device", "settings"),
    ("device", "public"),
    ("device", "detection"),
    ("device", "detection", "enable"),
    ("device", "detection", "disable"),
)


@pytest.mark.parametrize("path", PUBLIC_HELP_PATHS)
def test_every_public_help_path_is_actionable(path: tuple[str, ...]) -> None:
    result = CliRunner().invoke(cli.app, [*path, "--help"])

    assert result.exit_code == 0, result.output
    text = " ".join(result.stdout.split())
    assert "Example" in text
    for implementation_term in (
        "canonical profile",
        "immutable profile",
        "version-2",
        "value/token",
    ):
        assert implementation_term not in text.lower()


@pytest.mark.parametrize(
    "arguments",
    [
        ("sources",),
        ("version",),
        ("verify",),
        ("recover",),
        ("gc",),
        ("operations",),
        ("generate-synthetic",),
        ("benchmark-exact",),
        ("schema", "types"),
        ("acceleration", "clear"),
    ],
)
def test_pre_v1_commands_have_no_alias(arguments: tuple[str, ...]) -> None:
    result = CliRunner().invoke(cli.app, [*arguments, "--help"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_global_version_is_v1() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "1.0.0"


def test_bare_tty_uses_selected_workspace_and_empty_workspace_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    workspace = Workspace.create(tmp_path / "workspace")
    select_workspace(workspace.root)
    brands: list[bool] = []
    shells: list[Workspace] = []
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli, "_emit_brand", lambda: brands.append(True))
    monkeypatch.setattr(
        cli, "_run_shell", lambda selected, *, show_brand: shells.append(selected)
    )

    empty = CliRunner().invoke(cli.app, [])
    assert empty.exit_code == 0
    assert "Next: pandoracle import /path/to/data.csv" in empty.stdout
    assert brands == [True]
    assert shells == []

    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    import_csv(workspace, source, schema_plan=confirm_plan(analyze_csv(source)))
    ready = CliRunner().invoke(cli.app, [])
    assert ready.exit_code == 0
    assert shells[0].root == workspace.root
    assert brands == [True, True]


def test_search_json_has_no_branding_prompt_or_ansi(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("email\na@example.test\n", encoding="utf-8")
    workspace = Workspace.create(tmp_path / "workspace")
    import_csv(workspace, source, schema_plan=confirm_plan(analyze_csv(source)))

    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "a@example.test",
            "--workspace",
            str(workspace.root),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    value = json.loads(result.stdout)
    assert value["result_version"] == 1
    assert "hunt through private data" not in result.stdout
    assert "search >" not in result.stdout
    assert "\x1b[" not in result.stdout


def test_clean_xdg_cli_workflow_import_accelerate_search_and_inspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    runner = CliRunner()
    source = tmp_path / "contacts.csv"
    source.write_text("email\nperson@example.test\n", encoding="utf-8")
    schema_path = tmp_path / "contacts.schema.json"
    acceleration_path = tmp_path / "contacts.acceleration.json"

    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    analyzed = runner.invoke(
        cli.app,
        ["schema", "analyze", str(source), "--output", str(schema_path)],
    )
    assert analyzed.exit_code == 0
    schema = load_schema_document(schema_path)
    assert schema.schema_plan_version == 1
    write_json_atomic(schema_path, confirm_plan(schema).to_dict())
    imported = runner.invoke(
        cli.app,
        [
            "import",
            str(source),
            "--dataset",
            "contacts",
            "--schema",
            str(schema_path),
            "--format",
            "json",
        ],
    )
    assert imported.exit_code == 0

    planned = runner.invoke(
        cli.app,
        [
            "acceleration",
            "plan",
            "contacts",
            "--output",
            str(acceleration_path),
            "--format",
            "json",
        ],
    )
    assert planned.exit_code == 0
    plan = load_acceleration_plan(acceleration_path)
    assert plan.plan_version == 1
    write_json_atomic(acceleration_path, confirm_acceleration_plan(plan, {}).to_dict())
    configured = runner.invoke(
        cli.app,
        [
            "acceleration",
            "configure",
            "contacts",
            "--plan",
            str(acceleration_path),
            "--format",
            "json",
        ],
    )
    assert configured.exit_code == 0

    shell = runner.invoke(cli.app, ["shell"], input="person@example.test\n:quit\n")
    assert shell.exit_code == 0
    assert "person@example.test" in shell.stdout
    searched = runner.invoke(
        cli.app, ["search", "person@example.test", "--format", "json"]
    )
    record_ref = json.loads(searched.stdout)["records"][0]["record_ref"]
    inspected = runner.invoke(
        cli.app, ["inspect", record_ref, "--format", "json"]
    )
    assert inspected.exit_code == 0
    assert json.loads(inspected.stdout)["record"]["email"] == "person@example.test"

    obsolete = plan.to_dict()
    obsolete["acceleration_plan_version"] = 2
    write_json_atomic(acceleration_path, obsolete)
    with pytest.raises(ImportFailure, match="unsupported acceleration plan version"):
        load_acceleration_plan(acceleration_path)
