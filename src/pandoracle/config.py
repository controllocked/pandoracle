from __future__ import annotations

import json
import os
from pathlib import Path

from pandoracle.errors import WorkspaceError
from pandoracle.fs import write_json_atomic

CONFIG_VERSION = 1
ACTIVE_DEVICE_VERSION = 1


def config_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".config"
    return base / "pandoracle" / "config.json"


def default_workspace_path() -> Path:
    root = os.environ.get("XDG_DATA_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".local" / "share"
    return base / "pandoracle" / "workspace"


def runtime_directory() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value:
        raise WorkspaceError("XDG_RUNTIME_DIR is unavailable; open the device from a login session")
    root = Path(value) / "pandoracle"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def active_device_path() -> Path:
    return runtime_directory() / "active-device.json"


def active_device_workspace() -> Path | None:
    try:
        path = active_device_path()
    except WorkspaceError:
        return None
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if set(value) != {"active_device_version", "device_id", "pid", "workspace"}:
            raise ValueError("unknown or missing fields")
        if int(value["active_device_version"]) != ACTIVE_DEVICE_VERSION:
            raise ValueError("unsupported active-device version")
        pid = int(value["pid"])
        workspace = Path(str(value["workspace"]))
        if pid <= 0 or not workspace.is_absolute():
            raise ValueError("invalid active-device state")
        os.kill(pid, 0)
        if not workspace.exists():
            raise ProcessLookupError
        return workspace
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None


def set_active_device(*, device_id: str, workspace: Path, pid: int) -> None:
    write_json_atomic(
        active_device_path(),
        {
            "active_device_version": ACTIVE_DEVICE_VERSION,
            "device_id": device_id,
            "pid": pid,
            "workspace": str(workspace),
        },
    )


def clear_active_device(*, pid: int | None = None) -> None:
    try:
        path = active_device_path()
    except WorkspaceError:
        return
    if pid is not None and path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if int(value.get("pid", -1)) != pid:
                return
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    path.unlink(missing_ok=True)


def selected_workspace() -> Path | None:
    path = config_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if set(value) != {"config_version", "workspace"}:
            raise ValueError("unknown or missing fields")
        if int(value["config_version"]) != CONFIG_VERSION:
            raise ValueError("unsupported config version")
        workspace = str(value["workspace"])
        if not workspace:
            raise ValueError("workspace path is empty")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise WorkspaceError(
            f"invalid user config {path}: {error}; run 'pandoracle workspace PATH'"
        ) from error
    return Path(workspace)


def select_workspace(path: Path) -> Path:
    selected = path.expanduser().resolve(strict=True)
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    write_json_atomic(
        target,
        {"config_version": CONFIG_VERSION, "workspace": str(selected)},
    )
    return selected
