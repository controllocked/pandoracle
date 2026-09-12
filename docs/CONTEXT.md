# Engineering context

Pandoracle 1.0 is a Linux/XDG, offline-first, single-user dataset search tool. The
installed `pandoracle` command owns onboarding and workspace selection; the core
continues to accept an ordinary POSIX workspace path.

| Concern | Modules | Closest tests |
| --- | --- | --- |
| CLI, shell, branding | `cli.py`, `cli_ui.py`, `config.py`, `assets/` | `test_cli.py`, `test_config.py` |
| Workspace and catalog v5 | `workspace.py`, `catalog.py`, `fs.py` | `test_workspace.py` |
| Schema and semantic lifecycle | `schema.py`, `revision.py`, `contracts.py` | `test_schema_revision.py`, `test_semantic_types.py` |
| Import and publication | `ingest.py` | `test_ingest_search.py` |
| Acceleration | `acceleration.py`, `acceleration_profiles.py` | `test_universal_search.py` |
| Search and provenance | `universal_search.py`, `search.py` | `test_universal_search.py` |
| Recovery and GC | `maintenance.py` | `test_recovery.py`, `test_gc.py` |
| Pandora devices | `device_*.py` | `test_device_*.py` |

The write path confirms every field, locks the workspace, copies or reuses RAW,
builds record and canonical artifacts in staging, validates and fsyncs them, moves
them to final paths, then switches active catalog pointers in one last transaction.

The read path resolves active semantic/canonical/acceleration metadata, chooses an
available acceleration or projected scan, combines local ordinal sets, and reads
complete original records only for survivors. Search/storage architecture is frozen
for v1 surface cleanup.

The optional Linux Pandora layer discovers and provisions removable LUKS2 drives,
then supplies the mounted `workspace/` path to the unchanged core. Device identity,
credential roles, privilege boundaries, public/private separation, and supervised
session lifetime are specified by ADR 0008 and `SECURITY.md`.

Public pre-release compatibility has ended. Only catalog v4→v5 is supported; v1-v3
catalogs, old JSON plans, and EXACT-index workspaces are rejected. Do not reintroduce
fallback parsing or aliases.

See `DATABASE.md` for persistence work, `CLI_UX.md` for user-facing behavior,
`SECURITY.md` for claims, ADR 0005 for the three layers, and ADR 0007 for the v1
surface decision. Performance work still requires the documented 1 GB/10 GB
campaign; local smoke measurements are not acceptance evidence.
