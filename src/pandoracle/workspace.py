from __future__ import annotations

import fcntl
import json
import os
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from types import TracebackType

from pandoracle import __version__
from pandoracle.catalog import Catalog
from pandoracle.errors import WorkspaceError
from pandoracle.fs import fsync_directory, utc_now, write_json_atomic
from pandoracle.models import WorkspaceFormat, WorkspaceManifest

MANIFEST_NAME = "workspace.json"
CATALOG_NAME = "catalog.sqlite"
SUPPORTED_WORKSPACE_MAJOR = 1


class WorkspaceLock:
    def __init__(self, path: Path, *, exclusive: bool):
        self.path = path
        self.exclusive = exclusive
        self._file = None

    def __enter__(self) -> WorkspaceLock:
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self._file = os.fdopen(descriptor, "a+b")
        mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(self._file.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._file.close()
            raise WorkspaceError(
                "workspace is already in use by an incompatible session"
            ) from error
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()


class Workspace:
    def __init__(self, root: Path, manifest: WorkspaceManifest):
        self.root = root
        self.manifest = manifest
        self.catalog = Catalog(root / CATALOG_NAME)

    @classmethod
    def create(cls, path: str | Path) -> Workspace:
        requested = Path(path).expanduser()
        if requested.is_symlink():
            raise WorkspaceError("refusing to initialize a workspace through a symlink")
        root = requested.absolute()
        created_root = False
        if root.exists():
            if not root.is_dir():
                raise WorkspaceError(f"workspace target is not a directory: {root}")
            if any(root.iterdir()):
                raise WorkspaceError("workspace target must not exist or must be empty")
        else:
            root.mkdir(parents=True, mode=0o700)
            created_root = True

        manifest = WorkspaceManifest(
            manifest_schema_version=1,
            workspace_id=str(uuid.uuid4()),
            format=WorkspaceFormat(),
            created_at=utc_now(),
            created_by=f"pandoracle/{__version__}",
            minimum_reader_version=__version__,
        )
        try:
            for relative in (
                "objects/raw/sha256",
                "datasets",
                "canonical/v1",
                "accelerations/v1",
                "operations/staging",
                "operations/orphans",
                "operations/catalog-backups",
                "tmp",
            ):
                (root / relative).mkdir(parents=True, mode=0o700)
            write_json_atomic(root / MANIFEST_NAME, manifest.to_dict())
            Catalog(root / CATALOG_NAME).initialize(manifest.workspace_id)
            os.chmod(root / CATALOG_NAME, 0o600)
            lock_descriptor = os.open(root / ".pandoracle.lock", os.O_CREAT | os.O_RDWR, 0o600)
            os.close(lock_descriptor)
            fsync_directory(root)
        except Exception:
            if created_root:
                shutil.rmtree(root)
            raise
        return cls.open(root)

    @classmethod
    def open(cls, path: str | Path) -> Workspace:
        requested = Path(path).expanduser()
        if requested.is_symlink():
            raise WorkspaceError("refusing to open a workspace through a symlink")
        try:
            root = requested.resolve(strict=True)
        except OSError as error:
            raise WorkspaceError(
                f"workspace path is unavailable: {requested}: {error}; "
                "run 'pandoracle workspace PATH'"
            ) from error
        manifest_path = root / MANIFEST_NAME
        catalog_path = root / CATALOG_NAME
        if manifest_path.is_symlink() or catalog_path.is_symlink():
            raise WorkspaceError("workspace control files must not be symlinks")
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = WorkspaceManifest.from_dict(value)
            uuid.UUID(manifest.workspace_id)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise WorkspaceError(f"invalid workspace manifest: {error}") from error
        if manifest.manifest_schema_version != 1:
            raise WorkspaceError("unsupported workspace manifest schema")
        if manifest.format.major != SUPPORTED_WORKSPACE_MAJOR:
            raise WorkspaceError(f"unsupported workspace format major {manifest.format.major}")
        workspace = cls(root, manifest)
        migrated = workspace.catalog.migrate(
            root / "operations/catalog-backups", manifest.workspace_id
        )
        if migrated and manifest.minimum_reader_version != __version__:
            manifest = replace(manifest, minimum_reader_version=__version__)
            write_json_atomic(manifest_path, manifest.to_dict())
            workspace = cls(root, manifest)
        workspace.catalog.validate(manifest.workspace_id)
        return workspace

    def lock(self, *, exclusive: bool) -> WorkspaceLock:
        return WorkspaceLock(self.root / ".pandoracle.lock", exclusive=exclusive)

    def resolve_relative(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root):
            raise WorkspaceError("catalog artifact path escapes the workspace")
        return candidate
