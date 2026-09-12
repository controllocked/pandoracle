# ADR 0002: Immutable artifacts and catalog-last publication

Status: accepted

Imports write only to per-operation staging. Validated artifacts are moved to
immutable version paths, then made visible by one SQLite catalog transaction.
Partial operations never mutate the active dataset version.

There is no distributed transaction across SQLite and the filesystem. A crash
between artifact rename and catalog commit may create an invisible orphan;
recovery and later garbage collection handle it without guessing that it is
published.

Recovery is non-destructive: it makes stale journal/profile state terminal and
quarantines operation staging. Reclamation is a separate explicit command with
a deterministic dry run and reference checks. Every active or historical
published version/profile, its registered artifacts, referenced RAW, and stable
RecordRefs are protected.

Explicitly quarantined malformed CSV records are a third immutable version artifact.
They are staged, checksummed, fsynced, moved with the record tree, and registered by the
same catalog-last transaction. Accepted records alone receive contiguous ordinals;
reject source positions do not become RecordRefs. The content-addressed RAW remains the
byte-exact authority, while the reject JSON Lines artifact preserves the decoded raw
record and deterministic parse diagnostics for review.

Garbage collection itself follows a filesystem-first, catalog-last protocol.
Eligible objects are renamed into operation-local trash, incomplete catalog rows
are removed in one transaction, and trash is deleted after commit. Interrupted
GC is handled by ordinary recovery and another GC pass. Operation tombstones are
retained. This decision uses the existing catalog schema and single-writer lock.
