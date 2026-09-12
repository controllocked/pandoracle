class PandoracleError(Exception):
    """Base error suitable for presentation to a CLI user."""


class WorkspaceError(PandoracleError):
    """Workspace is missing, invalid, incompatible, or unsafe to use."""


class ImportFailure(PandoracleError):
    """An import could not be completed and was not published."""


class SearchFailure(PandoracleError):
    """A query could not be executed safely."""


class DeviceError(PandoracleError):
    """A Pandora removable-device operation could not be completed safely."""


class DeviceBusyError(DeviceError):
    """A safe device close is temporarily blocked by an active user."""
