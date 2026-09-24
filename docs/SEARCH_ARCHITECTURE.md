# Search architecture

## Executive summary

Pandoracle implements offline semantic retrieval over heterogeneous, immutable
dataset versions. A query describes meaning rather than source column names; the
engine resolves that meaning against every active dataset, chooses an access path
independently for each dataset, verifies candidates against canonical values, and
materializes complete original records only for the final survivors.

The architecture deliberately separates three concerns:

1. **Semantic meaning** — what a source field represents.
2. **Canonical representation** — how values of that meaning are normalized and
   compared.
3. **Physical access** — whether a query uses a disposable acceleration segment or
   a projected Parquet scan.

This separation is the central search invariant. Removing every acceleration file
may reduce performance, but it must not make a published dataset unsearchable or
change its record identities.

## Design objectives

- Search all active datasets through one query contract.
- Preserve exact source provenance and return complete original records.
- Keep query planning deterministic and independent of argument order.
- Avoid reading unrelated columns or materializing non-surviving records.
- Make acceleration optional, immutable, independently rebuildable, and safe to
  discard.
- Bound in-memory row sets and require explicit permission for predicted expensive
  scans.
- Record only non-sensitive performance measurements, never query values or result
  contents.

The engine is not a relevance-ranked document search system. It does not provide
fuzzy matching, prefix search, transliteration, entity resolution, token order, or
cross-field token composition. Those capabilities would require new explicit,
versioned contracts.

## Search data model

| Layer | Immutable object | Responsibility | Physical representation |
| --- | --- | --- | --- |
| Source | RAW blob | Byte-exact input evidence | Content-addressed file by SHA-256 |
| Records | DatasetVersion | Original accepted values and stable ordinals | Parquet |
| Meaning | SemanticSchema | Field identity and confirmed semantic type | Catalog recipe |
| Comparison | CanonicalProfile | Canonicalizer, tokenizer, ordering, and supported operators | Aligned Parquet sidecar |
| Access | AccelerationProfile | Optional per-field `VALUE` and `TOKEN` policies | Independent SQLite segments |

Record and canonical Parquet row groups are aligned. Internal record columns use
stable names such as `f_0000`; canonical columns use names such as `n_0000`.
Original headers and their semantic mappings live in the versioned schema recipe,
so invalid, duplicated, or hostile source headers cannot become storage identifiers.

The public record identity is always:

```text
RecordRef(dataset_version_id, record_ordinal)
```

Parquet row groups, row offsets, and SQLite posting rows are implementation details
that can be rebuilt without changing a `RecordRef`.

## Query contract

A `SearchRequest` contains one or more clues, an optional dataset restriction, a
global result limit, and an explicit permission bit for expensive scans.

```text
SearchRequest
  ├── clue: semantic type + operator + value
  ├── clue: semantic type + operator + value
  ├── dataset: optional name or UUID
  ├── limit: 1..10,000
  └── allow_expensive_scan: boolean
```

Clues are combined with **AND**. Within a clue, all fields carrying the requested
semantic type are combined with **OR**. This gives consistent behavior across files
whose column names and layouts differ.

Supported logical operators are:

| Operator | Semantics | Physical primitive |
| --- | --- | --- |
| `EXACT` | Canonical equality | `VALUE` or scan |
| `RANGE` | Inclusive canonical interval; currently ordered ISO dates | `VALUE` or scan |
| `TOKEN` | Every distinct query token occurs in the same source field | `TOKEN` or scan |
| `AUTO` | Resolve through the versioned field contract | One of the above |

`AUTO` is deterministic. Names normally resolve to `TOKEN`; identifiers resolve to
`EXACT`; a four-digit date or year range resolves to an inclusive date `RANGE`; and a
custom `exact-text/v1` type uses the default chosen in its declaration.

## End-to-end execution

### 1. Resolve a consistent search snapshot

The engine acquires a shared workspace lock and reads active pointers from the
catalog. For every selected dataset it verifies that:

- the active DatasetVersion exists and is `PUBLISHED`;
- record artifacts exist;
- the active CanonicalProfile belongs to that DatasetVersion and is published;
- canonical artifacts exist;
- the active AccelerationProfile belongs to both the DatasetVersion and the active
  CanonicalProfile and is published.

A broken or cross-linked active pointer is a hard error. The engine does not guess
which artifact was intended.

### 2. Bind clues to semantic fields

Each clue is resolved against all canonical fields of the same semantic type in the
dataset. A dataset missing any requested semantic type is marked `not_applicable` and
cannot produce a result for that request.

The operator is resolved and validated against the versioned field contract. Query
values are canonicalized with the same canonicalizer ID and version that produced
the dataset sidecar. Invalid values fail before physical planning.

### 3. Assess acceleration coverage

For a clue to use an acceleration primitive as a seed, every compatible field in the
dataset must be covered by that primitive and the corresponding immutable artifact
must exist. Partial coverage is never used as a complete seed because doing so could
silently omit records from an uncovered field.

Acceleration segments contain one `WITHOUT ROWID` postings table keyed by:

```text
(field_id, canonical_key COLLATE BINARY, record_ordinal)
```

`VALUE` serves exact and ordered range lookup. `TOKEN` groups by field and ordinal
and requires all distinct query tokens in that same field. Segments also bind their
format version, primitive, dataset version, canonical profile, acceleration profile,
and policy fingerprint; verification rejects metadata mismatches.

### 4. Build bounded candidate row sets

Usable seeds are ranked by observed cardinality, then by stable semantic type and
operator tie-breaks. The most selective set starts the plan. Additional sets are
intersected while the candidate population exceeds 250,000 rows.

Candidate sets are sorted, unique Arrow `uint64` arrays. A single set is capped at
64 MiB of ordinals, and retained cross-dataset candidate state is bounded. An
overflow falls back to scan planning instead of allowing unbounded memory growth.

For a one-clue accelerated request, lookup is limited to the global result limit
plus one, which supports truncation detection without loading the complete posting
list.

### 5. Compare candidate filtering with a projected scan

The planner estimates both paths:

- a full scan of only the relevant canonical columns plus `record_ordinal`; and
- when candidates exist, reads of only the row groups containing those candidates.

The estimate includes startup cost, compressed projected bytes, operator-row
evaluations, and a 25% safety margin. Baselines are progressively calibrated by
local samples containing workload name, row count, byte count, and elapsed time.
Query values, matched values, and result records are never performance samples.

The candidate path is selected only when its predicted cost is lower. `--scan` does
not force a scan; it grants permission to execute whichever deterministic plan was
chosen.

Predicted scan work requires explicit permission when total estimated duration
exceeds ten seconds. Before a usable local model exists, conservative bootstrap
gates also protect scans above 5,000,000 rows or 256 MiB of projected compressed
data.

### 6. Evaluate canonical values

PyArrow performs vectorized comparison over the canonical sidecar:

- full plans iterate projected columns in 65,536-row batches;
- candidate plans read only candidate row groups and `take` only candidate offsets;
- fields within one clue are OR-combined;
- clues are AND-combined.

Even accelerated candidates are verified against canonical data. The index is an
access path, not the source of result semantics.

Independent dataset plans may execute with up to four worker threads when progress
reporting and candidate recomputation do not require serial execution. Pandoracle
remains single-writer; this bounded read parallelism does not create concurrent
publication.

### 7. Apply the global limit before record materialization

Each dataset scan stops after `limit + 1` matches. Survivors from all datasets are
then ordered by dataset name and record ordinal; a merged population above the
global limit marks the result as truncated. Only the selected `limit` records are
fetched from original-record Parquet.

For each selected ordinal, aligned record and canonical row groups produce:

- the complete original record;
- a stable `RecordRef`;
- dataset and source identity;
- per-clue match metadata;
- original and canonical field values needed for provenance;
- the physical access used (`VALUE`, `TOKEN`, candidate filtering, or scan).

Results are deliberately unranked. Their deterministic ordering does not imply a
confidence score.

## Planning invariants

1. Search semantics come from the active CanonicalProfile, not current application
   defaults.
2. Query argument order cannot change the chosen plan.
3. Partial index coverage cannot become a seed and create false negatives.
4. Acceleration failure or removal cannot make a DatasetVersion unsearchable.
5. Original records are read only for global survivors.
6. Physical locations never escape as public record identities.
7. Planner telemetry contains sizes and timings, not sensitive query content.
8. A plan that exceeds policy limits fails closed until the user explicitly permits
   the predicted scan.

## Acceleration lifecycle

Acceleration is configured after DatasetVersion publication. The builder reads only
published canonical Parquet, writes a new profile under operation staging, validates
SQLite integrity and binding metadata, checksums and fsyncs the artifacts, moves
them to an immutable final directory, and switches the active catalog pointer last.

An identical configuration may deduplicate. Rebuild forces a new generation.
Disable publishes an empty profile. A failed build leaves the prior profile active
and the dataset scan-searchable.

## Observability

`search --explain --format json` exposes a machine-readable execution report with:

- resolved access per clue;
- complete, partial, or absent acceleration coverage;
- cardinality-reduction steps;
- full-scan and chosen-plan estimates;
- estimate basis and confidence;
- projected bytes and operator evaluations;
- candidate, scanned, returned, and truncation counts;
- actual scan and materialization timing.

This makes planner decisions auditable without logging the query itself.

## Diagram specification

A useful architecture diagram should contain four horizontal lanes:

1. **Query contract** — request, clue validation, AUTO resolution, canonicalization.
2. **Planner** — coverage check, cardinality ranking, row-set intersection, cost
   comparison, permission gate.
3. **Physical data** — immutable VALUE/TOKEN SQLite segments, canonical Parquet,
   original-record Parquet.
4. **Result** — global merge, `limit + 1`, materialization, provenance.

Required decision diamonds:

- semantic type present in the dataset?
- complete acceleration coverage?
- row set within the memory bound?
- candidate row-group plan cheaper than full projected scan?
- estimated scan within policy or explicitly permitted?

Use solid arrows for record flow, dashed arrows for metadata/control decisions, and
visually mark record Parquet as accessed only after survivor selection.

## Implementation map

| Responsibility | Primary module |
| --- | --- |
| Query validation, planning, scans, and materialization | `src/pandoracle/universal_search.py` |
| Immutable posting segments and row-set intersection | `src/pandoracle/acceleration.py` |
| Acceleration planning, build, validation, and activation | `src/pandoracle/acceleration_profiles.py` |
| Semantic contracts and custom type resolution | `src/pandoracle/contracts.py` |
| Canonicalizers | `src/pandoracle/normalize.py` |
| Request, result, provenance, and profile models | `src/pandoracle/models.py` |
| Active pointers and local performance samples | `src/pandoracle/catalog.py` |

The normative architectural decisions are recorded in
[ADR 0005](adr/0005-three-layer-profiles.md) and
[ADR 0006](adr/0006-universal-search-and-acceleration.md).
