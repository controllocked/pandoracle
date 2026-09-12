# ADR 0003: Parquet canonical artifacts and external search indexes

Status: superseded by ADR 0005 and ADR 0006

Normalized records are stored in immutable Parquet shards. Exact point lookups
use a separate immutable SQLite segment per dataset version. The catalog stores
only segment metadata.

The `ExactIndex` behavior is replaceable before the 100 GB milestone. Search and
provenance contracts cannot expose SQLite-specific identifiers.
