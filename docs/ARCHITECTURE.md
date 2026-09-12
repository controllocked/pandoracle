# Architecture

## Product responsibility

Pandoracle searches all active datasets by default through semantic indexes,
resolves matching hits to complete records, and returns stable references and
provenance. The user decides what to do with a result outside the core search
engine. The supported human interface is a conventional CLI; inline prompts are
allowed, but a full-screen TUI is not planned.

## Core boundary

The Pandoracle core receives an accessible POSIX directory. It does not inspect
block devices, mount filesystems, handle encryption credentials, or elevate
privileges. The Pandora device adapter remains outside this boundary and hands the
core only the mounted private workspace path.

```text
UDisks2 + LUKS2 + Secret Service
               │ mounted POSIX path
               ▼
        device supervisor ── physical removal ──► terminate shell group
               │
               ▼
        unchanged CLI / core
```

```text
CLI / shell
    │
    ▼
Workspace + Catalog
    ├── schema proposal → explicit user confirmation
    ├── CSV ingestion → immutable RAW
    ├── schema revision → managed RAW reuse
    ├── SemanticSchema   → original-record Parquet
    ├── CanonicalProfile → aligned canonical Parquet sidecar
    └── AccelerationProfile → optional VALUE/TOKEN SQLite segments
                               │
SearchRequest ─────────────────┘
    ↓
SearchResult v1 + matches + provenance + original records
```

## Stable identities

- `workspace_id`: random UUID stored in both manifest and catalog.
- `dataset_id`: stable logical UUID independent of a source filename.
- `dataset_version_id`: immutable import result.
- `canonical_profile_id`: immutable canonicalizer snapshot for one dataset version.
- `index_profile_id`: immutable access-path policy for one canonical profile.
- `source_sha256`: identity of exact RAW bytes, not logical dataset identity.
- `RecordRef`: `(dataset_version_id, record_ordinal)`.

`record_ordinal` is assigned contiguously in accepted parsed-source order. A malformed
record keeps its source record number and physical line span in the reject artifact but
does not consume an ordinal. A physical locator points to a Parquet part, row group, and
row offset, but is a rebuildable index detail and must never replace `RecordRef` in a
public result.

## Workspace layout

```text
workspace.json
catalog.sqlite
objects/raw/sha256/<digest>
datasets/<dataset-id>/<version-id>/data/part-*.parquet
datasets/<dataset-id>/<version-id>/rejects/rejected-records.jsonl
canonical/v1/<canonical-profile-id>/part-*.parquet
accelerations/v1/<index-profile-id>/value.sqlite
accelerations/v1/<index-profile-id>/token.sqlite
operations/staging/<operation-id>/
operations/orphans/<operation-id>/
operations/catalog-backups/catalog-v<schema>-*.sqlite
tmp/
```

The manifest is intentionally small and strict. SQLite stores transactional
metadata. Search postings live in per-version index segments so the catalog does
not scale with record count and the backend remains replaceable.

Record Parquet uses internal original field names (`f_0000`, and so on).
Canonical sidecars use `n_0000` names and row groups aligned with the record
artifact. Original
headers and confirmed semantic mappings live in the versioned schema recipe. This avoids
collisions, invalid identifiers, and accidental coupling to source naming.

Inference produces a `FieldProposal`, never a publishable `FieldSpec`. A
versioned `SchemaPlan` records ordered headers, parse recipe, evidence,
user-selected type IDs, and optional custom-type declarations. Samples are
ephemeral UI data and are deliberately absent from persisted plans and catalog.
Custom types are workspace-scoped immutable definitions using `exact-text/v1`.
They declare their own AUTO default (`EXACT` or Unicode-whitespace `TOKEN`). A
CanonicalProfile snapshots that choice together with supported operators,
tokenizer, ordering, and canonicalizer versions. Their lifecycle state is mutable:
retirement hides a definition from new assignments without invalidating an existing
dataset contract, and restoration makes it selectable again.

## Publication protocol

1. Analyze the source and obtain explicit confirmation of every field before
   copying RAW or creating an operation.
2. Create a durable operation journal record.
3. Copy and hash RAW into operation staging.
4. Revalidate exact headers/parse recipe and record the confirmed schema
   fingerprint.
5. In one RAW pass, stream accepted original and canonical Parquet row groups, collect
   canonical/token-sample statistics, and write explicitly quarantined malformed records
   to a staged JSON Lines artifact without repair.
6. Validate accepted row counts, aligned row groups, reject metadata, and file checksums.
7. `fsync` files/directories and rename artifacts to immutable final paths.
8. In one catalog transaction, register artifacts and custom types, switch the
   active version, and attach an empty active acceleration profile.

A crash before step 8 leaves the previous active version unchanged. Explicit
recovery marks stale journal state terminal and quarantines staging without
deleting evidence. Reference-aware GC inventories catalog and managed storage,
protects every active or historical published version, and reclaims abandoned
RAW, staging, quarantine, and final artifacts only after an explicit apply.

GC uses the same catalog-last principle. Eligible filesystem objects move into
an operation-local trash tree before one catalog transaction removes incomplete
metadata. Trash is deleted only after commit. A crash during GC therefore leaves
either the original references or quarantinable trash; recovery plus another GC
is idempotent. FAILED and ABORTED operation rows remain as audit tombstones.

A `SCHEMA_REVISION` follows the same catalog-last protocol but reads the managed
RAW blob and does not copy it. It creates a new version linked by
`parent_version_id`; any failure leaves the previous active version untouched.
Deduplication is keyed by source bytes, parse recipe, confirmed schema, and
normalizer versions rather than source hash alone.

## Universal search responsibilities

Semantic meaning, canonical normalization, and optional acceleration are
separate immutable layers. Every canonical field is searchable with no
acceleration. The only physical primitives are:

- `VALUE`, canonical value to local ordinal, including ordered ISO-date ranges;
- `TOKEN`, canonical token to local ordinal for tokenizer-enabled contracts;
- `SCAN`, projected vectorized PyArrow evaluation of canonical columns.

`SearchRequest` AND-combines clues. Compatible fields within one clue are ORed.
AUTO is resolved by the versioned field contract: names default to TOKEN, dates
resolve years to inclusive ranges and full dates to EXACT, identifiers default
to EXACT, and custom types use their declaration. Explicit operators override
AUTO. TOKEN requires every distinct token in one field and provides no prefix,
substring, fuzzy, transliteration, ordering, or cross-field semantics.

The deterministic rule planner accepts an acceleration seed only when every
compatible field is covered. It orders complete seeds by cardinality and a
stable semantic tie-break, intersects nonselective sets, and compares bounded
candidate row-group reads with a full projected scan. Local row sets are sorted
unique Arrow `uint64` arrays. Original Parquet and provenance are materialized
only for global `limit + 1` survivors. Results are unranked and ordered by
dataset name and record ordinal.

Projected scans below the estimated-cost policy run automatically. Scans whose
conservative predicted duration exceeds ten seconds require `--scan`; fixed
row/byte gates apply only before a throughput model is usable. Estimates use
compressed projected bytes, candidate row groups, operator evaluations, and
the median of the last five qualifying local non-sensitive observations.
`--scan` grants permission for the chosen plan; it never disables acceleration.

Acceleration configuration reads only published canonical Parquet. It builds a
staged immutable profile, validates SQLite integrity and metadata, checksums and
fsyncs artifacts, then atomically switches the catalog pointer. Identical
configuration deduplicates, rebuild forces a new generation, disable publishes an
empty profile, and failure leaves the previous profile and scan-searchable
DatasetVersion active.

Entity resolution and fuzzy indexes will be derived, versioned layers. Removing
them must never affect source records or exact provenance.

Search results are information, not external actions. Messaging, enforcement,
case management, and other downstream workflows stay outside the core boundary.
