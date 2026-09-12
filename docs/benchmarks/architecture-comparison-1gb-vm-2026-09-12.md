# 1 GB architecture comparison on the development VM (2026-09-12)

## Status

This is a real 1 GB RAW diagnostic campaign, not the M2 acceptance result. The
corpus is larger than 1,000,000,000 bytes, but the host is a KVM development VM
using an internal virtual SATA disk. It does not match the reference external
USB 3.x SSD machine in the benchmark protocol. No 10 GB or interruption matrix
was run.

The benchmark driver is
[`scripts/benchmark_architecture.py`](../../scripts/benchmark_architecture.py).
Raw JSON, resource measurements, corpus manifest, and checksums are under the
ignored local directory
`benchmark-results/architecture-1gb-2026-09-12/`.

## Environment and corpus

- Pandoracle 1.0.0, Python 3.14.6, PyArrow 25.0.1, SQLite 3.53.4, DuckDB 1.5.5.
- GNU grep 3.12, ripgrep 15.2.0, GNU Awk 5.3.2.
- Kali GNU/Linux Rolling, kernel 7.0.12, 6 vCPUs reported as Intel Core Ultra 7
  155U, 10.6 GiB RAM.
- `/dev/sda1`, virtual SATA, rotational flag set, ext4,
  `rw,relatime,errors=remount-ro`; no LUKS layer was reported.
- Seed `20260912`; four shards of 540,000 records; 2,160,000 records total;
  1,017,493,456 RAW bytes (1.017 GB decimal).
- Each record has a skewed person name, high-cardinality email, DOB over a
  deterministic 40-year cycle, phone, city, IP, and 384 bytes of deterministic
  UNKNOWN payload. Email has one shared value per 100,000 rows. Each shard has
  one rare `Zephyr Benchmark` name. Ground-truth cardinalities are stored in
  `ground-truth.json`.

All data is deterministic and synthetic. No personal data is present.

## Method

The same manifest drove all engines. Workloads covered present, absent, and
duplicate exact email values; frequent, rare, and two-token names; one- and
five-year DOB ranges; a three-clue AND query; and four-dataset fan-out.

Pandroacle timings call the search API repeatedly in one process and include
record and provenance materialization, but not JSON serialization. DuckDB uses
one process and `fetchall()`, either directly over CSV or over a materialized
table with email and DOB indexes. grep/ripgrep/awk use a fresh process per trial
and return raw CSV lines without parsing, semantic normalization, stable record
references, or provenance. Competitors probe up to 51 rows so truncation at the
Pandroacle limit of 50 can be detected.

Warm p95 is the empirical nearest-rank percentile. Single-shard iteration counts
were 30 for Pandoracle scan and grep/ripgrep, 20 for awk and DuckDB CSV, and 50
for Pandoracle acceleration and DuckDB table. Fan-out used 10-40 trials depending
on engine cost. This asymmetry is retained in the raw results and means close
numbers should not be over-interpreted.

The kernel page cache was not globally dropped. Warm runs followed explicit
warmups. A separate three-trial sanity check used `POSIX_FADV_DONTNEED` on each
relevant file before every trial; those results are called *advised-cold*, never
cold-cache results.

## Build and storage results

Importing all four shards took 50.78 s, or 42,538 rows/s and 19.11 MiB/s of RAW
input. Per-shard time ranged from 11.04 to 15.10 s. Peak import RSS was 284,904
KiB. A repeated representative import measured a 253,985,921-byte staging
high-water mark; no staging entries remained after publication.

| Operation on one 540k shard | Predicted | Actual | Artifact | Postings | Peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| VALUE: email + DOB | 19.84 s | 2.62 s | 36.62 MB | 1.08 M | 181,004 KiB |
| TOKEN: person name | 15.13 s | 2.26 s | 18.90 MB | 1.08 M | 182,712 KiB |
| Combined | 17.15 s | 6.43 s | 55.52 MB | 2.16 M | 204,816 KiB |

The combined profile over all four shards took 28.86 s versus 31.81 s predicted
(-9.3% aggregate error), created 222,035,968 bytes, and peaked at 206,148 KiB.
Individual prediction errors ranged from -62.5% to +90.8%, so the aggregate is
better than the per-dataset stability.

DuckDB loaded all CSV files in 27.67 s and built its two indexes plus statistics
in 3.18 s. Its database is 600,584,192 bytes and build peak RSS was 1,459,496
KiB. This is not contract-equivalent: it does not retain immutable source bytes,
separate canonical artifacts, Pandoracle provenance, or publication history.

| Active Pandoracle layer | Bytes | RAW ratio |
| --- | ---: | ---: |
| Immutable RAW | 1,017,493,456 | 100.0% |
| Complete-record Parquet | 170,183,822 | 16.7% |
| Canonical projected Parquet | 20,000,426 | 2.0% |
| VALUE + TOKEN acceleration | 222,035,968 | 21.8% |
| Active total | 1,429,713,672 | 140.5% |

The physical workspace occupied 1,485,415,436 bytes because it also retained
the separately built historical VALUE and TOKEN profiles. This validates the
replaceable-profile design, while also showing why GC/history policy matters.

## Warm single-shard latency

Values are p95 milliseconds. A dash means that the engine cannot express that
workload with the benchmarked primitive.

| Workload | Pando scan | Pando indexed | grep | ripgrep | awk | DuckDB CSV | DuckDB table |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Exact absent | 59.66 | 3.63 | 80.16 | 23.41 | — | 148.85 | 1.52 |
| Exact present, unique | 101.94 | 53.87 | 103.17 | 26.47 | — | 153.50 | 10.83 |
| Exact present, 6 rows | 252.57 | 225.60 | 88.61 | 23.73 | — | 210.36 | 11.07 |
| Token frequent, limit 50 | 91.66 | 120.60 | 4.02 | 6.35 | — | 85.11 | 6.23 |
| Token rare | 234.22 | 43.05 | 85.53 | 54.80 | — | 229.73 | 26.36 |
| Two-token rare | 237.21 | 44.67 | 57.78 | 57.91 | — | 236.32 | 25.47 |
| DOB, one year, limit 50 | 49.10 | 56.75 | — | — | 8.56 | 97.60 | 8.26 |
| DOB, five years, limit 50 | 54.45 | 63.13 | — | — | 7.43 | 89.06 | 5.63 |
| Email + token + DOB | 224.93 | 80.06 | — | — | 628.50 | 172.38 | 10.71 |

The canonical scan reads only relevant columns: the exact-absent query projects
2,343,115 compressed bytes rather than scanning the shard's roughly 254 MB RAW
file. That separation is the main reason Pandroacle scan remains competitive
despite returning parsed records and provenance.

The index is highly effective for absent and rare values. It is not universally
faster: frequent TOKEN and early-satisfying range queries regress because a
sequential canonical batch reaches 50 hits quickly, while the indexed path pays
SQLite rowset and Parquet materialization overhead.

## Four-dataset fan-out latency

| Workload | Pando scan | Pando indexed | grep | ripgrep | awk | DuckDB CSV | DuckDB table |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Exact absent | 201.10 | 19.79 | 280.89 | 72.30 | — | 368.49 | 1.08 |
| Exact shared, 24 rows | 930.79 | 765.81 | 274.33 | 76.01 | — | 410.05 | 4.71 |
| Token rare, 4 rows | 616.82 | 134.87 | 229.01 | 124.90 | — | 463.80 | 63.81 |
| Mixed, 1 row | 651.66 | 259.25 | — | — | 1,687.75 | 480.19 | 22.90 |

Pandroacle parallelizes the four segment scans and preserves deterministic
ordering. The fan-out scan peaked at 472,032 KiB RSS; indexed fan-out peaked at
333,644 KiB. The indexed shared-value case remains slow because 24 scattered
full records must be materialized from Parquet row groups.

## Memory and process-start effects

Representative warm-process peaks were 400,368 KiB for single-shard Pandoracle
scan, 304,388 KiB for indexed Pandoracle, 677,464 KiB for DuckDB CSV, and 172,944
KiB for the DuckDB table. Child-process VmRSS peaked near 2.6 MiB for grep, 249
MiB for ripgrep, and 4.2 MiB for awk. File-backed mappings contribute to the
ripgrep RSS number.

Pandroacle's fresh CLI process is much slower than its resident search engine.
Across 20 Hyperfine trials, p95 was 379 ms for indexed exact-absent, 399 ms for
indexed rare TOKEN, and 400 ms for indexed fan-out exact-absent. Corresponding
in-process p95 values were 3.63, 43.05, and 19.79 ms. Python/PyArrow startup is
therefore material for one-shot CLI usage; the interactive shell amortizes it.

The advised-cold three-trial medians for exact-absent were 72.83 ms (Pandroacle
scan), 27.08 ms (Pandroacle indexed), 277.50 ms (grep), and 247.68 ms (ripgrep).
For rare TOKEN they were 167.45, 81.26, 235.36, and 277.46 ms respectively.
These are diagnostic medians with too few samples for acceptance percentiles.

## Correctness and release findings

- Scan and acceleration produced identical ordered RecordRefs, counts, and
  truncation flags for every shared single-shard and fan-out workload.
- Reversing all three mixed-query clues preserved the result and selected VALUE
  seed path. Setting `allow_expensive_scan` on an accelerated request did not
  force a scan.
- Full workspace verification succeeded: 4 RAW blobs, 18 artifacts, and
  1,485,234,952 bytes verified. The additional two artifacts are retained
  historical one-primitive profiles. There were no import or build failures.
- `execution.actual_seconds` is not end-to-end: acceleration rowset lookup occurs
  before its timer, and materialization is reported separately. For one mixed
  indexed trial the segment fields totalled about 31.7 ms while external query
  latency was about 61 ms. This makes explain output unsuitable for latency SLOs
  until index lookup and total wall time are exposed explicitly.
- Scattered full-record materialization is the dominant indexed-query cost. The
  six-row exact workload spent roughly 134 ms in materialization in the saved
  explain sample. Smaller record row groups or a rebuildable ordinal-to-record
  access layer should be benchmarked before changing the durable format.
- The planner should account for LIMIT and selectivity when choosing between a
  canonical scan and an available accelerator. Current acceleration regressed
  p95 by 31.6% on the frequent TOKEN workload and by 15.6-15.9% on the range
  workloads.
- DuckDB's materialized table is the latency leader on this corpus. Pandoracle's
  advantages are bounded memory during build, immutable RAW retention, semantic
  normalization, stable RecordRefs, provenance, replaceable access paths, and
  catalog-last publication—not raw query speed. Release messaging must preserve
  that distinction.

## Reproduction outline

```console
python scripts/benchmark_architecture.py generate benchmark-results/run/corpus
python scripts/benchmark_architecture.py init benchmark-results/run/workspace
python scripts/benchmark_architecture.py import benchmark-results/run/workspace \
  benchmark-results/run/corpus/benchmark-00.csv bench-00
python scripts/benchmark_architecture.py profile benchmark-results/run/workspace \
  bench-00 both
python scripts/benchmark_architecture.py pandoracle-search \
  benchmark-results/run/workspace benchmark-results/run/corpus/manifest.json
python scripts/benchmark_architecture.py external-search \
  benchmark-results/run/corpus/manifest.json grep /usr/bin/grep
python scripts/benchmark_architecture.py duckdb-prepare \
  benchmark-results/run/corpus/manifest.json benchmark-results/run/benchmark.duckdb
```

The exact commands, iteration counts, external resource output, and SHA-256
checksums are retained with the raw campaign artifacts.
