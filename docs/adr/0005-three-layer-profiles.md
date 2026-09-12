# ADR 0005: Separate semantic, canonical, and index profiles

Status: accepted; physical search details superseded by ADR 0006 and ADR 0007

## Context

Assigning a semantic type previously implied both normalization and exact
postings. On wide, large datasets this made semantic description itself carry a
large import-time and storage cost, even for fields that were rarely queried.

## Decision

Pandoracle has three immutable layers:

1. `SemanticSchema` describes what original fields mean and belongs to an
   immutable DatasetVersion.
2. `CanonicalProfile` snapshots the type-owned canonicalizer ID/version and
   materializes aligned canonical values in a separate Parquet sidecar.
3. `IndexProfile` selects optional access paths. The first policy contract is
   `NONE|EXACT` and references one CanonicalProfile.

A semantic revision creates a DatasetVersion. Canonical and index revisions
preserve the dataset version, record ordinals, and RecordRefs. An index rebuild
reads canonical values rather than RAW or originals. `UNKNOWN + EXACT` is
invalid because UNKNOWN has no canonicalizer.

Policy proposals use per-field selections, explicit workspace preferences, and
built-in suggestions in that order. Repeated choices are not observed or
learned automatically.

## Consequences

- A typed `NONE` field remains present in complete records and in the canonical
  sidecar. The saving is postings, SQLite build work, and index storage—not
  canonicalization CPU.
- Search normalizes per active CanonicalProfile, so profile versions may coexist.
- `NONE` never triggers an implicit scan. Only migrated profiles explicitly
  marked for the narrow legacy person-name compatibility path may scan.
- Exact index v1 remains readable; new profiles use v2 metadata.
- Catalog v3 backfills legacy published versions without rewriting record-scale
  artifacts and switches active profile pointers only catalog-last.
