# ADR 0006: Universal search and disposable acceleration

- Status: accepted
- Date: 2026-09-05
- Supersedes: the physical-index choices in ADR 0003 and the “NONE never
  scans” consequence in ADR 0005
- Superseded in part: pre-v1 compatibility behavior by ADR 0007

## Context

The exact-only index made access-path configuration part of whether data was
searchable. Candidate search then added a second planner, row-wise Python
filters, special name handling, and explicit scans based on index presence.
This exposed storage choices to users and coupled DatasetVersion publication to
the success of a disposable index build.

Pandoracle needs one offline semantic retrieval contract across heterogeneous
active datasets. Semantic meaning and canonical values are durable; an access
path is optional derived state.

## Decision

Search has three physical primitives only:

- `VALUE`: canonical value to dataset-local record ordinal. It serves equality
  and, only with a declared binary lexical ordering, inclusive ranges.
- `TOKEN`: canonical token to dataset-local record ordinal. Every distinct
  query token must occur in the same source field.
- `SCAN`: projected PyArrow evaluation over canonical Parquet. It is universal
  and remains available with an empty acceleration profile.

Each CanonicalProfile snapshots supported operators, AUTO default, tokenizer,
ordering, and canonicalizer versions for every field. Built-ins own their AUTO
defaults. A custom `exact-text/v1` type explicitly chooses `EXACT` or `TOKEN`.

Each DatasetVersion has independent immutable acceleration profiles under
`accelerations/v1/<profile-id>/`. A profile contains zero, one, or two SQLite
files (`value.sqlite`, `token.sqlite`). Both use one `WITHOUT ROWID` postings
table keyed by `(field_id, key COLLATE BINARY, record_ordinal)`. New postings do
not store semantic types or Parquet coordinates. Sorted unique PyArrow `uint64`
arrays are the in-process row-set representation, capped at 64 MiB per set and
256 MiB across scheduled work.

The deterministic planner requires complete same-semantic-field coverage before
using an accelerator as a seed, orders seeds by cardinality and a stable
semantic tie-break, intersects while candidates exceed 250,000, and compares
candidate row-group reads with a full projected scan. Query argument order does
not select a plan. Original Parquet is read only for the global `limit + 1`
survivors.

Scan permission is based primarily on a conservative estimated duration:
startup plus projected compressed bytes/read rate plus operator-row evaluations
per measured kernel rate, with a 25% safety margin. The last five qualifying,
non-sensitive local samples calibrate rates. Fixed row and byte gates are only
bootstrap safeguards. `--scan` authorizes a chosen expensive scan and never
forces one.

Import and schema revision publish records, canonical data, statistics, and an
empty active profile first. Acceleration configuration is a later catalog-last
operation. It builds and validates staged files, moves them to an immutable
final directory, then switches `active_index_profile_id` in one catalog
transaction. Failure cannot make the DatasetVersion unsearchable. Rebuild
forces a generation, clear publishes an empty profile, and GC removes
superseded artifacts while retaining `RETIRED` profile specifications.

## Consequences

- Search availability no longer depends on acceleration.
- There is one query/result/explain path and no ranking or query-order behavior.
- Adding or removing a dataset never rebuilds a workspace-global index.
- Legacy EXACT v1/v2 segments remain readable as VALUE segments; no
  record-scale migration occurs during catalog upgrade.
- SchemaPlan no longer carries acceleration choices. Old versioned plans and
  v1 search response shapes are intentionally rejected rather than aliased.
- SQLite remains the postings backend and PyArrow remains the scan/row-set
  engine. Native Rust/C is not introduced without a representative benchmark
  proving an isolated missing hot path.
