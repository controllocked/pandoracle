# ADR 0001: Path-based workspace and stable logical identities

Status: accepted

The data core accepts a mounted POSIX path and has no block-device or encryption
API. Workspace, dataset, dataset version, source blob, and source record have
separate identities. A physical record locator is always rebuildable.

This permits the same core to operate in a development directory, a manually
mounted encrypted SSD, or a future launcher-managed session.
