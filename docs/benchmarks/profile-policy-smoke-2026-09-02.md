# Index-profile policy smoke — 2026-09-02

This is a small implementation smoke check, **not** the documented 1 GB
acceptance benchmark and not a general performance claim. It used deterministic
synthetic data only; no Kazakhstan or other real workspace was opened.

## Environment and workload

- Pandoracle 0.3.0; Python 3.14.6; PyArrow 25.0.1; SQLite 3.53.4.
- Linux 7.0.12+kali-amd64 in KVM, 6 visible CPU cores, Intel Core Ultra 7 155U.
- `/tmp` tmpfs; cache state uncontrolled; one run per configuration.
- 50,000 CSV rows, 5,317,024 RAW bytes, seven semantic/canonical fields:
  `PERSON_NAME`, `EMAIL`, `PHONE`, `USERNAME`, `IP`, `DOMAIN`, and `DATE`.
- Both imports performed the same canonicalization. Only the number of fields
  with the pre-v1 exact-value policy changed. Peak RSS is process `ru_maxrss`.

## Results

| Metric | 3 EXACT | 7 EXACT |
| --- | ---: | ---: |
| Total import | 7.097 s | 5.877 s |
| Observed transform span | 6.882 s | 5.644 s |
| Peak RSS | 241,596 KiB | 298,140 KiB |
| Postings | 150,000 | 350,000 |
| Record Parquet | 688,375 B | 688,375 B |
| Canonical Parquet | 688,572 B | 688,572 B |
| Exact index | 6,623,232 B | 14,303,232 B |

The single-run time ordering is inconclusive because the cache was uncontrolled;
it must not be used to claim that more indexes are faster. The acceptance signal
for this smoke is structural: record and canonical sizes stayed identical,
postings rose from 3 to 7 per valid row, exact-index storage increased, and peak
RSS increased. A separate policy-only rebuild from 3 to 7 fields took 1.029 s,
created 350,000 postings, and preserved both DatasetVersion and CanonicalProfile.

The separately authorized Kazakhstan run from the implementation plan was not
performed because this task explicitly excludes touching the real workspace.
