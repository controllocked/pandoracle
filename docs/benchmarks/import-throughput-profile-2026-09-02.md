# Import throughput diagnosis — 2026-09-02

This is a diagnostic report, not the 1 GB acceptance benchmark. It uses only
deterministic generated data. No production storage behavior, SQLite PRAGMA, or
real workspace state was changed.

## Environment and workload

- Pandoracle 0.3.0; Python 3.14.6; PyArrow 25.0.1; SQLite 3.53.4.
- Linux 7.0.12 under KVM; 6 visible Intel Core Ultra 7 155U CPUs; 10 GiB RAM.
- Source and temporary workspaces were under `/tmp`, which the captured
  `findmnt` metadata identifies as `tmpfs`. An earlier draft incorrectly called
  this ext4; the raw baseline JSON is authoritative. Page cache was not dropped,
  so all results are explicitly uncontrolled/warm-cache measurements.
- Seven canonical fields: `fullname`, `DATE`, `inn`, `PHONE`, `citizenship`,
  `nationality`, and `address`.
- Only `fullname`, `inn`, and `PHONE` used `EXACT` index policy.
- Seed `20260902`; the 1,310,720-row source was 411,566,144 bytes. Generation was
  timed separately and excluded from import time.

The pre-v1 instrumentation harness was removed at the v1 compatibility boundary;
this report remains historical diagnostic evidence, not a current benchmark recipe.
It wraps the existing importer in-process and restores every patched symbol
after the run. `cProfile` ran separately from throughput measurements.

## Pre-fix baseline

Three low-overhead 200,000-row runs were stable enough to avoid repeating the
large run:

| Run | Import wall | Rows/s | Process CPU / wall |
| --- | ---: | ---: | ---: |
| 1 | 50.412 s | 3,967 | 0.997 |
| 2 | 53.805 s | 3,717 | 0.999 |
| 3 | 50.473 s | 3,963 | 1.000 |

The wall-time coefficient of variation was 3.07%. The separate profiled run
took 129.433 s; its time is not used as throughput.

The 1,310,720-row run took 276.705 s, or 4,737 rows/s, with process CPU/wall
0.999 and peak RSS 342,980 KiB. It created 3,932,160 postings.

## Pre-fix successive intervals

Each full interval contains 65,536 rows and 196,608 postings.

| Batch | Rows complete | Batch wall | Rows/s | SQLite add | B-tree pages |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 65,536 | 11.392 s | 5,753 | 0.351 s | 2,120 |
| 2 | 131,072 | 11.659 s | 5,621 | 0.629 s | 4,256 |
| 3 | 196,608 | 11.290 s | 5,805 | 0.551 s | 6,408 |
| 4 | 262,144 | 10.668 s | 6,143 | 0.315 s | 8,561 |
| 5 | 327,680 | 10.774 s | 6,083 | 0.511 s | 10,709 |
| 6 | 393,216 | 10.123 s | 6,474 | 0.536 s | 12,859 |
| 7 | 458,752 | 11.332 s | 5,784 | 0.485 s | 14,983 |
| 8 | 524,288 | 12.326 s | 5,317 | 0.694 s | 17,116 |
| 9 | 589,824 | 12.636 s | 5,187 | 0.586 s | 19,241 |
| 10 | 655,360 | 13.283 s | 4,934 | 0.550 s | 21,368 |
| 11 | 720,896 | 14.757 s | 4,441 | 0.684 s | 23,497 |
| 12 | 786,432 | 16.526 s | 3,966 | 0.668 s | 25,623 |
| 13 | 851,968 | 16.821 s | 3,896 | 0.719 s | 27,723 |
| 14 | 917,504 | 16.122 s | 4,065 | 0.729 s | 29,818 |
| 15 | 983,040 | 16.380 s | 4,001 | 0.708 s | 31,965 |
| 16 | 1,048,576 | 16.155 s | 4,057 | 0.669 s | 34,119 |
| 17 | 1,114,112 | 15.603 s | 4,200 | 0.674 s | 36,275 |
| 18 | 1,179,648 | 15.386 s | 4,259 | 0.638 s | 38,433 |
| 19 | 1,245,184 | 14.771 s | 4,437 | 0.668 s | 40,571 |
| 20 | 1,310,720 | 14.467 s | 4,530 | 0.672 s | 42,659 |

The first-five median was 5,805 rows/s and the last-five median was 4,259
rows/s: a confirmed 26.62% decline under the predefined 15% threshold.

SQLite add median increased from 0.511 s to 0.669 s, or 30.8%, while the index
grew to 164.96 MiB before finalization. That increase explains only about 0.16 s
of a roughly 3–5 s batch slowdown. Throughput also recovered in batches 17–20
while the B-tree continued growing, so accumulated SQLite size is a secondary,
not primary, cause at this scale.

## Pre-fix stage attribution

Low-overhead timers from the large run are nested and therefore do not sum to
100%. Percentages use total import wall time unless stated otherwise.

| Stage | Wall | Import % |
| --- | ---: | ---: |
| RAW copy + SHA-256 | 0.495 s | 0.179% |
| CSV analysis | 2.173 s | 0.785% |
| Transform, total | 273.858 s | 98.971% |
| Arrow table conversion | 1.672 s | 0.604% |
| Record Parquet writes | 1.453 s | 0.525% |
| Canonical Parquet writes | 1.391 s | 0.503% |
| SQLite `executemany` | 12.037 s | 4.350% |
| SQLite commit/integrity/close/fsync | 1.332 s | 0.481% |
| Three artifact checksums | 0.156 s | 0.056% |
| Nine catalog transactions | 0.009 s | 0.003% |
| Application fsync wrappers | 0.002 s | 0.001% |

`cProfile` attribution from the separate 200,000-row run:

- `_build_artifacts`: 127.741 s cumulative.
- four `flush` calls: 126.651 s cumulative.
- 1,400,000 useful `normalize_with` calls: 118.863 s cumulative, 93.05% of
  `_build_artifacts`.
- `flush` Python self-time, including list/statistics/posting construction:
  3.085 s, 2.42% of `_build_artifacts`.
- `_build_artifacts` self-time, including CSV iteration/batch fill and setup:
  0.784 s, 0.61%.
- SQLite `executemany`: 2.163 s cumulative in the profiled run.

The pre-fix `normalize_with` reconstructed its dispatcher for every value. To
discover IDs it called all eight built-in normalizers with an empty string
before calling the selected normalizer. For 200,000 rows this produced:

- 1,400,000 useful dispatches;
- 11,200,000 probe normalizer calls;
- 12,600,000 `NormalizedValue` objects;
- 600,000 each of `Posting`, `PhysicalLocator`, and SQLite parameter tuples.

At 1,310,720 rows the corresponding counts are 9,175,040 useful dispatches,
73,400,320 probes, 82,575,360 normalized-value objects, and 3,932,160 objects of
each posting-related kind.

A five-round paired microbenchmark alternated current and precomputed dispatch
order. The median wall and CPU speedup for canonicalization was 10.65x. Per-round
wall speedups were 9.53x, 10.42x, 10.65x, 10.89x, and 14.96x. This is a local
counterfactual measurement, not a claim that the entire import will improve by
the same factor.

## Post-fix validation

The confirmed fix replaced the per-value dispatcher reconstruction with one
module-level immutable canonicalizer-ID lookup. No normalizer, schema contract,
SQLite setting, Parquet layout, batch size, transaction, or durability behavior
changed. The generated artifact sizes were byte-for-byte identical between the
large baseline and post-fix profiles.

### Overall throughput

| Run | Before wall | Before rows/s | After wall | After rows/s | Speedup |
| --- | ---: | ---: | ---: | ---: | ---: |
| 200k #1 | 50.412 s | 3,967 | 8.970 s | 22,297 | 5.62x |
| 200k #2 | 53.805 s | 3,717 | 11.219 s | 17,827 | 4.80x |
| 200k #3 | 50.473 s | 3,963 | 11.178 s | 17,892 | 4.52x |
| 1,310,720 | 276.705 s | 4,737 | 64.141 s | 20,435 | 4.31x |

The three post-fix 200k runs averaged 19,339 rows/s. Their wall-time
coefficient of variation was 10.05%, below the predefined 15% repeat threshold.
The separate post-fix cProfile run took 15.437 s; it is excluded from throughput
figures. Peak RSS in the large run remained effectively unchanged: 342,980 KiB
before and 343,688 KiB after.

### Post-fix successive intervals

| Batch | Rows complete | Batch wall | Rows/s | SQLite add | B-tree pages |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 65,536 | 2.695 s | 24,317 | 0.588 s | 2,120 |
| 2 | 131,072 | 2.842 s | 23,057 | 0.543 s | 4,256 |
| 3 | 196,608 | 2.892 s | 22,663 | 0.666 s | 6,408 |
| 4 | 262,144 | 3.057 s | 21,440 | 0.681 s | 8,561 |
| 5 | 327,680 | 2.761 s | 23,735 | 0.687 s | 10,709 |
| 6 | 393,216 | 2.743 s | 23,892 | 0.641 s | 12,859 |
| 7 | 458,752 | 3.051 s | 21,477 | 0.694 s | 14,983 |
| 8 | 524,288 | 3.101 s | 21,136 | 0.665 s | 17,116 |
| 9 | 589,824 | 2.963 s | 22,117 | 0.672 s | 19,241 |
| 10 | 655,360 | 2.689 s | 24,369 | 0.477 s | 21,368 |
| 11 | 720,896 | 3.337 s | 19,639 | 0.743 s | 23,497 |
| 12 | 786,432 | 3.110 s | 21,075 | 0.691 s | 25,623 |
| 13 | 851,968 | 3.098 s | 21,155 | 0.694 s | 27,723 |
| 14 | 917,504 | 3.194 s | 20,517 | 0.735 s | 29,818 |
| 15 | 983,040 | 2.984 s | 21,960 | 0.700 s | 31,965 |
| 16 | 1,048,576 | 3.088 s | 21,220 | 0.696 s | 34,119 |
| 17 | 1,114,112 | 3.202 s | 20,469 | 0.721 s | 36,275 |
| 18 | 1,179,648 | 3.105 s | 21,108 | 0.664 s | 38,433 |
| 19 | 1,245,184 | 2.890 s | 22,680 | 0.693 s | 40,571 |
| 20 | 1,310,720 | 2.893 s | 22,651 | 0.654 s | 42,659 |

The first-five median was 23,057 rows/s and the last-five median was 21,220
rows/s. The decline is now 7.97%, below the 15% threshold, so accumulated-state
degradation is not confirmed after the dispatch fix. SQLite add median changed
only from 0.666 s in the first five batches to 0.693 s in the last five, a 4.0%
increase while the B-tree grew to the same 42,659 pages.

### Before/after stage attribution

Timers are nested and do not sum to 100%. Percentages are relative to the total
large import wall time.

| Stage | Before wall (% import) | After wall (% import) |
| --- | ---: | ---: |
| RAW copy + SHA-256 | 0.495 s (0.179%) | 0.523 s (0.816%) |
| CSV analysis | 2.173 s (0.785%) | 2.509 s (3.911%) |
| Transform, total | 273.858 s (98.971%) | 60.926 s (94.989%) |
| Arrow table conversion | 1.672 s (0.604%) | 1.868 s (2.912%) |
| Record Parquet writes | 1.453 s (0.525%) | 1.374 s (2.142%) |
| Canonical Parquet writes | 1.391 s (0.503%) | 1.346 s (2.099%) |
| SQLite `executemany` | 12.037 s (4.350%) | 13.304 s (20.743%) |
| SQLite commit/integrity/close/fsync | 1.332 s (0.481%) | 1.186 s (1.848%) |
| Three artifact checksums | 0.156 s (0.056%) | 0.163 s (0.254%) |
| Nine catalog transactions | 0.009 s (0.003%) | 0.008 s (0.013%) |
| Application fsync wrappers | 0.002 s (0.001%) | 0.002 s (0.003%) |

SQLite's relative share increased because the dominant CPU bug disappeared;
its absolute time did not improve and its last-five batch growth is small. No
SQLite configuration or policy was changed in this work.

The separate 200k cProfile shows the causal change:

- `normalize_with`: 118.863 s before versus 7.950 s after, a 14.95x reduction
  in cumulative time including the selected normalizers;
- dispatcher self-time: 7.555 s before versus 0.546 s after;
- `strptime`: 4,409,737 calls before versus 209,737 after;
- `_build_artifacts`: 127.741 s before versus 13.662 s after;
- probe normalizer calls: 11,200,000 before versus zero after;
- `NormalizedValue` objects: 12,600,000 before versus 1,400,000 after.

The remaining `normalize_with` cumulative time is real normalization of the
seven canonical fields, not dispatcher reconstruction.

## CPU versus I/O

Both runs are CPU-bound:

- process CPU averaged 99.89% before and 99.88% after in `pidstat`;
- process CPU/wall was 0.999 before and 0.999 after;
- average scheduler wait was 0.16% before and 0.17% after, while process
  `iodelay` stayed zero;
- process block-I/O delay ticks stayed zero;
- post-fix system `%iowait` was normally zero and the `tmpfs` workload produced
  no attributable block-device rows; any captured `sda` activity is unrelated
  host activity;
- no major page faults occurred.

The pre-fix interval decline was real, but it fell below the confirmation
threshold once the dispatcher regression was removed. The post-fix profile does
not support attributing a multi-fold slowdown to accumulated SQLite size.

## Confirmed pre-fix bottlenecks and regression

1. **Canonicalizer dispatch was the primary regression.** It accounted for 93%
   of profiled transform and performed eight unrelated normalizations per useful
   value. The paired cached lookup was 10.65x faster.
2. **Python per-value construction is secondary.** Seven canonical fields and
   three indexed fields create tens of millions of short-lived dataclasses and
   parameter tuples. `flush` self-time was 2.42% under `cProfile`.
3. **SQLite B-tree insertion is measurable but secondary.** It used 4.40% of
   low-overhead transform time and degraded mildly as pages accumulated. It is
   not the source of the multi-fold regression at 1.31 million rows.

There are no per-batch catalog transactions, commits, index queries, repeated
source reads, or hot-path checksums. The index uses one transaction and one
`executemany` per 65,536-row batch. Checksums and integrity validation occur
after transformation. Catalog work occurs only at phase/publication boundaries.

The documented earlier implementation used direct semantic-type dispatch,
normalized only three recognized/indexed fields, and wrote original and
normalized columns into one Parquet artifact. The current implementation
canonicalizes all seven typed fields and writes separate record/canonical
Parquet artifacts. The latter two changes add work, but measured Parquet writes
were only 1.04% of total; the accidental dispatcher rebuild is the clear
behavioral regression. Repository commit history is unavailable, so this is a
documentation/current-code comparison rather than a commit diff.

## Follow-up boundary

The immutable canonicalizer lookup is now implemented and validated. No second
optimization was included. SQLite add now represents 21.84% of transform time,
but the first/last interval decline is only 7.97% and does not correlate with a
meaningful rise in absolute insert time. Any callable prebinding or SQLite
experiment requires a separate profiling task and explicit authorization.

## Aborted import cleanup

Read-only inspection after profiling confirmed that the real workspace was not
changed. Operation `4824fb39-e7aa-4534-8282-53005f811675` remains
`TRANSFORMING`, its DatasetVersion remains unpublished, staging still exists,
and there are no artifact or profile rows.

The supported recovery command is:

```console
.venv/bin/pandoracle maintenance recover \
  --workspace /srv/pandoracle-benchmark/workspace \
  --format json
```

At the time of this diagnostic it marked interrupted state `ABORTED` and moved
staging to `operations/orphans`, but did not reclaim space. The registered 5,315,002,912-byte
RAW blob, aborted DatasetVersion, empty `kz_db` dataset, operation journal row,
and 400,539,138 bytes of quarantined staging remain. Manual deletion would make
catalog references inconsistent. Workspace-level index preferences are not
owned by the aborted operation and must be preserved.

The later M2 implementation adds supported `pandoracle maintenance gc` dry-run/apply
reclamation. This historical workspace was not mutated while preparing either
report; it must be recovered and reviewed with a GC dry run before any apply.

## Reproduction status

The exact pre-v1 harness is intentionally not reproducible against v1 because it
patched the retired index writer. New campaigns use the supported helpers under
`scripts/` and must publish their own complete commands and environment details.
