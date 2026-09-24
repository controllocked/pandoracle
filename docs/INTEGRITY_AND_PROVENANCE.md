# Data integrity, provenance, and crash-safe publication

## Executive summary

Pandoracle treats imported data as evidence rather than mutable application rows.
The exact source bytes, accepted original records, canonical search values, malformed
record diagnostics, and every published revision have distinct roles and identities.
Search acceleration is disposable; provenance is not.

The most important durability rule is **filesystem first, catalog last**. Pandoracle
fully builds, validates, checksums, syncs, and moves immutable artifacts before one
SQLite transaction changes the active dataset pointers. A crash may leave an
unreferenced artifact, but it cannot expose a partially published version as active.

## Why this architecture exists

SQLite can make catalog changes transactional, and a POSIX filesystem can atomically
rename within a filesystem, but there is no transaction spanning both. Treating them
as one atomic system would create a failure window in which metadata points at
missing or incomplete files.

Pandoracle instead uses a monotonic visibility protocol:

```text
private staging → validated immutable artifacts → durable final paths
                                                    ↓
                                      one catalog commit point
                                                    ↓
                                              active version
```

Visibility moves in one direction. Before the catalog commit, new data is not active.
After the commit, all referenced files have already reached their final paths.

## Identity model

| Identity | Meaning | Stability |
| --- | --- | --- |
| `workspace_id` | One logical workspace | Stored in strict manifest and catalog |
| `dataset_id` | One logical named dataset | Stable across revisions and versions |
| `dataset_version_id` | One immutable import or schema revision result | Never reused for different content |
| `source_sha256` | Identity of exact RAW bytes | Content-addressed and verifiable |
| `canonical_profile_id` | One immutable canonicalization contract and sidecar | May change without changing record identity |
| `index_profile_id` | One immutable acceleration policy/build | Disposable and replaceable |
| `operation_id` | One journaled mutation attempt | Retained as an audit tombstone |
| `RecordRef` | Dataset version plus accepted-record ordinal | Public stable record reference |

The canonical record identity is:

```text
RecordRef(dataset_version_id, record_ordinal)
```

`record_ordinal` is contiguous in accepted parsed-source order. Parquet part, row
group, row offset, and SQLite posting coordinates are rebuildable physical locators,
not public identity.

## Storage roles

| Store | Content | Mutability | Authority |
| --- | --- | --- | --- |
| `objects/raw/sha256/<digest>` | Byte-exact input | Immutable | Source evidence |
| `datasets/<dataset>/<version>/data/` | Accepted original records | Immutable | Returned record values |
| `datasets/<dataset>/<version>/rejects/` | Quarantined malformed records and diagnostics | Immutable | Parse-failure evidence |
| `canonical/v1/<profile>/` | Aligned canonical search values | Immutable profile; replaceable layer | Search comparison |
| `accelerations/v1/<profile>/` | VALUE/TOKEN postings | Immutable profile; disposable layer | Search access only |
| `catalog.sqlite` | Identities, recipes, checksums, statuses, and active pointers | Transactional | Publication state |
| `operations/staging/<operation>/` | In-progress private build | Mutable until finalized | Never active |
| `operations/orphans/<operation>/` | Recovered interrupted staging | Quarantined | Diagnostic/reclaimable |

The catalog stores metadata rather than record-scale postings. This keeps the
transactional control plane small and prevents a physical index backend from becoming
part of the provenance contract.

## Import preconditions

Schema inference is advisory. No RAW copy or operation is started until every field
has an explicit confirmed semantic choice, including the valid `UNKNOWN` choice.

Before mutation, import performs a capacity and accessibility preflight and resolves
the source path. The workspace is single-writer and held under an exclusive lock for
the publication workflow.

The confirmed plan contains:

- exact ordered headers;
- CSV dialect, encoding, and parse recipe;
- semantic field IDs and storage names;
- canonicalizer IDs and versions;
- optional workspace custom semantic type declarations;
- explicit malformed-record policy.

Samples used during schema review are ephemeral UI data. They are not stored in the
plan or catalog.

## Import publication protocol

### Phase 1: journal and RAW acquisition

1. Allocate operation, dataset-version, canonical-profile, and empty
   acceleration-profile UUIDs.
2. Create a mode-`0700` operation staging directory.
3. Create or resolve the logical dataset and durably journal an `IMPORT` operation.
4. Copy the source once while computing SHA-256 and byte count.
5. If the content-addressed RAW object already exists, verify its hash and size before
   reuse; otherwise atomically rename the staged copy into the RAW object store and
   sync the parent directory.
6. Re-read RAW to validate headers and the confirmed parse recipe.

The managed RAW object, not the caller's path, becomes the byte-exact authority.

### Phase 2: deterministic transformation

In one streaming RAW pass, Pandoracle:

- parses records in source order;
- assigns contiguous ordinals only to accepted records;
- writes original values to record Parquet;
- canonicalizes confirmed searchable fields into an aligned sidecar;
- collects non-sensitive normalization and acceleration-planning statistics;
- writes explicitly quarantined malformed records to JSON Lines without repairing
  them.

Transformation uses 65,536-row batches. Record and canonical artifacts are fsynced
before they can be finalized.

### Phase 3: validation

Before any active pointer can change, Pandoracle verifies:

- accepted row counts match both Parquet artifacts;
- record and canonical row-group counts and row counts align exactly;
- reject artifact count and structure match the recorded diagnostics;
- cryptographic digests and sizes for every artifact can be recorded;
- immutable final targets do not already exist.

Validation failure marks the attempt failed without changing the active version.

### Phase 4: filesystem publication

Record/reject and canonical staging trees are atomically renamed to their immutable
final directories. Parent directories are synced after each rename. At this point
the files are durable but intentionally invisible to normal reads because no catalog
pointer references them yet.

### Phase 5: catalog commit point

One SQLite transaction:

- registers every artifact with kind, format version, relative path, SHA-256, and
  size;
- registers custom semantic types if needed;
- publishes the CanonicalProfile;
- publishes an empty AccelerationProfile so search remains universally available;
- marks the DatasetVersion `PUBLISHED` with row and quarantine counts;
- switches the dataset's active version, canonical profile, and acceleration profile;
- marks the operation `PUBLISHED`.

SQLite uses foreign keys, `BEGIN IMMEDIATE` for write transactions, a five-second
busy timeout, `synchronous=FULL`, and DELETE journal mode. The transaction is the
only visibility commit point.

## Failure matrix

| Failure point | Observable state | Safety property | Follow-up |
| --- | --- | --- | --- |
| Before operation journal | No managed mutation | Existing dataset untouched | Retry normally |
| During RAW copy | Staging may be incomplete | No active pointer changed | Recovery quarantines staging |
| After RAW finalization | Unreferenced or referenced content-addressed RAW may exist | RAW is verified before reuse | GC reclaims only when unreferenced |
| During transformation | Partial staged Parquet/reject files | Staging is never active | Recovery marks operation terminal and quarantines |
| After validation, before final rename | Complete staging | Existing version remains active | Recover and inspect or reclaim |
| After final rename, before catalog commit | Complete immutable orphan | No catalog reader can select it | Reference-aware GC identifies it |
| During catalog transaction | SQLite rolls back the whole visibility change | Active pointer is old or fully new, never mixed | Reopen and recover |
| After catalog commit | New version is fully published | Cleanup failure cannot undo success | Later recovery/GC handles harmless residue |

## Malformed-record provenance

Malformed input is never silently repaired. Depending on the confirmed policy,
import aborts or writes a versioned `CSV_REJECTS` artifact.

A rejected record preserves:

- decoded raw record content;
- original source record number;
- physical line span;
- deterministic parse diagnostics.

It does not consume a `record_ordinal`, so stable references remain contiguous over
accepted records. The reject artifact is checksummed, fsynced, moved with the dataset
tree, and registered by the same catalog-last transaction as accepted records.

## Result provenance

Search does not return index rows. After planning and canonical verification, it
materializes complete original records from record Parquet and attaches:

- `RecordRef`;
- dataset ID and name;
- DatasetVersion and exact source identity;
- matching field identities;
- original and canonical comparison values;
- access-path metadata.

The same reference can be inspected independently of the physical path used to find
it. Rebuilding canonical or acceleration profiles does not rewrite the source record
or change its ordinal.

## Schema revision and deduplication

A schema revision reuses managed RAW and follows the same staging, validation,
filesystem-first, catalog-last protocol. It publishes a new DatasetVersion linked to
its predecessor by `parent_version_id`; the prior version remains immutable.

Import deduplication is not based on source hash alone. Reuse requires the same:

- logical dataset;
- RAW bytes;
- parse recipe and malformed-record policy;
- confirmed semantic schema fingerprint;
- relevant normalizer versions.

The distinction prevents semantically different interpretations of identical bytes
from collapsing into one version.

## Recovery

Recovery is conservative and non-destructive:

1. Acquire the exclusive workspace lock.
2. Refuse unsafe managed roots or symlinked staging entries.
3. Mark non-terminal operation rows `ABORTED`.
4. Mark their unpublished DatasetVersions `ABORTED` without touching published ones.
5. Mark inactive unpublished canonical and acceleration profiles `FAILED`.
6. Commit those catalog transitions.
7. Move remaining staging directories into uniquely named `operations/orphans/`
   paths and sync both directories.

Recovery does not guess that a file was successfully published, delete evidence, or
repoint active datasets.

## Reference-aware garbage collection

Garbage collection is a separate explicit action and defaults to a read-only plan.
Inventory covers catalog references and every managed root. Mutation is blocked by
unsafe paths, symlinks, missing published artifacts, broken active pointers, or other
conditions that make reachability ambiguous.

Apply re-plans under the exclusive lock, journals a `GC` operation, and then:

1. renames eligible filesystem objects into operation-local trash;
2. syncs source and trash parents after every move;
3. removes incomplete metadata and retires obsolete profiles in one catalog
   transaction;
4. marks the GC operation published;
5. deletes trash only after the catalog commit.

If GC crashes before its commit, catalog references still describe the pre-GC state
and the trash tree is recoverable. If it crashes after commit, the metadata state is
authoritative and leftover trash can be removed later. Re-running recovery and GC is
therefore idempotent.

Published historical versions, their artifacts, referenced RAW objects, and stable
RecordRefs remain protected. `FAILED` and `ABORTED` operation rows are retained as
audit tombstones even after reclaimable bytes are removed.

## Catalog migration discipline

Catalog format changes are additive and versioned. A supported migration first
creates and fsyncs a mode-`0600` SQLite backup under
`operations/catalog-backups/`, performs the change in one transaction, advances the
schema version, and must reopen as a no-op on the new version.

Unsupported pre-release layouts are rejected rather than guessed or partially
upgraded. Identity preservation is more important than permissive recovery.

## Integrity versus confidentiality

These mechanisms provide publication consistency, stable identity, checksummed
artifact verification, and recoverable failure behavior. They do not encrypt an
ordinary directory and do not authenticate storage against a malicious writer with
host access.

Confidentiality at rest is supplied by the optional Pandora LUKS2 device layer or by
an independently protected containing filesystem. The separation keeps storage
durability claims testable without embedding device or privilege assumptions in the
data core.

## Diagram specification

Three diagrams expose the architecture clearly.

### Publication timeline

Use parallel lanes for `source`, `staging`, `final filesystem`, and `SQLite catalog`.
Mark hash/copy, transformation, validation, fsync, atomic rename, and the final
catalog transaction. Draw a vertical commit line at the active-pointer switch and
show that every crash point to its left leaves the old version active.

### Provenance graph

Use nodes for RAW SHA-256, DatasetVersion, SemanticSchema, record Parquet,
CanonicalProfile, canonical Parquet, AccelerationProfile, and SearchResult. Draw
`RecordRef` from DatasetVersion plus ordinal, and visually distinguish durable
evidence from replaceable derived layers.

### Recovery and GC state machine

Show operation states `PLANNED → COPYING_RAW → ANALYZING → TRANSFORMING → VALIDATING
→ PUBLISHED`, with interruptions converging on `ABORTED`/`FAILED` and staged data
moving to `orphans`. A separate GC flow should move candidates to trash, commit
metadata, and only then delete bytes.

## Implementation map

| Responsibility | Primary module |
| --- | --- |
| Import, RAW acquisition, transformation, and publication | `src/pandoracle/ingest.py` |
| Catalog schema and transactional metadata | `src/pandoracle/catalog.py` |
| Atomic writes, hashing, copying, and fsync helpers | `src/pandoracle/fs.py` |
| Workspace identity and shared/exclusive locking | `src/pandoracle/workspace.py` |
| Schema plans and semantic fingerprints | `src/pandoracle/schema.py` |
| Schema revision | `src/pandoracle/revision.py` |
| Recovery, verification, and garbage collection | `src/pandoracle/maintenance.py` |
| Stable identities and provenance models | `src/pandoracle/models.py` |

The compact normative contracts are in [DATABASE.md](DATABASE.md),
[ADR 0001](adr/0001-workspace-and-identities.md), and
[ADR 0002](adr/0002-immutable-publication.md).
