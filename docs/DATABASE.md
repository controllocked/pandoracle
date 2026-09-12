# Database and storage contracts

## Storage roles

| Store | Role | Mutability |
| --- | --- | --- |
| `catalog.sqlite` | workspace metadata and active pointers | transactional |
| `objects/raw/sha256/` | byte-exact source evidence | immutable |
| `datasets/.../*.parquet` | original imported records | immutable |
| `canonical/v1/.../*.parquet` | aligned canonical values | replaceable derived data |
| `accelerations/v1/.../*.sqlite` | optional exact-value or whole-token postings | replaceable derived data |

Record-scale postings never belong in the catalog. Stable public identity is
`RecordRef(dataset_version_id, record_ordinal)`, never a physical Parquet or SQLite
coordinate.

## Catalog schema v5

The catalog stores workspace metadata, datasets and versions, source blobs,
operations, artifacts, canonical and acceleration profiles, performance samples,
and custom semantic types. `custom_semantic_types.retired_at` is null for an active
type and timestamped for a retired type. Built-in types are not catalog rows.

Fresh v5 catalogs do not create pre-v1 exact-index or preference tables. An upgraded
v4 catalog may retain an unused old table as inert migration residue, but runtime
search, verification, and garbage collection never read it.

Foreign keys are enabled. Writes use `BEGIN IMMEDIATE`, a five-second busy timeout,
`synchronous=FULL`, and DELETE journal mode. Pandoracle is intentionally
single-writer.

## Supported migration

Only the final development catalog v4 upgrades to v5. Before mutation, Pandoracle
creates and fsyncs a mode-`0600` SQLite backup under
`operations/catalog-backups/`. One transaction adds `retired_at`, resets stored
acceleration policy/metrics markers to public v1, and updates the catalog version.
Dataset, version, profile, artifact, and workspace identities are preserved. Opening
the resulting v5 catalog again is a no-op.

Catalog v1-v3 and any workspace containing an `EXACT_INDEX` artifact are rejected
with instructions to create a new v1 workspace. There is no repair, backfill, or
copy-upgrade path for pre-release data.

## Publication invariant

The filesystem and SQLite cannot share one transaction. Every import, revision, or
acceleration build therefore follows this order:

1. build below `operations/staging/<operation-id>/`;
2. validate counts, structure, and checksums;
3. fsync files and directories;
4. atomically move artifacts to final paths;
5. commit metadata and active pointers in one catalog transaction.

The catalog pointer is switched last. A failure before that commit cannot replace
the prior active dataset or acceleration.

## Recovery and collection

Managed roots are `objects/raw/sha256`, `datasets`, `canonical/v1`,
`accelerations/v1`, `operations/staging`, and `operations/orphans`. Recovery marks
interrupted operations terminal and moves staging entries to orphans. GC verifies
references, protects all published dataset versions and their record references,
and only retires/reclaims unreachable derived state. Symlinks, unsafe paths, broken
active pointers, and missing published artifacts block mutation.

## Change checklist

Catalog changes require an additive migration, schema version bump, durable backup,
transactional update, reopen/no-op test, identity preservation test, and failure
safety test. Do not change workspace identity, publication ordering, SQLite
ownership, or the single-writer model without an ADR and representative benchmarks.
