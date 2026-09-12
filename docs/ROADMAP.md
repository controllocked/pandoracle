# Roadmap status

## Pandoracle 1.0 surface

The v1 implementation includes XDG workspace selection, `pandoracle` onboarding,
the interactive search shell, confirmed CSV schemas, immutable source/record
publication, complete provenance, custom semantic type retirement/restoration,
optional exact-value and whole-token acceleration, record inspection, recovery,
verification, reference-aware garbage collection, JSON automation, and a
distributable wheel.

The Pandora device layer adds a dedicated removable-drive workflow around the
unchanged path-based core: UDisks2/polkit lifecycle management, LUKS2/ext4 private
storage, an owner-supplied public file hierarchy, independent owner/recovery/per-host
credentials, Secret Service auto-unlock, XDG insertion detection, and a shell bound
to physical device presence. Hardware and GNOME/KDE release acceptance remain open.

The public CLI and JSON contracts deliberately start at v1. Pre-release command
aliases, plan formats, compatibility parsing, exact-index artifacts, and catalog
v1-v3 migration code are not supported. Catalog v4→v5 is the sole development
upgrade path.

## Remaining performance milestone

The separate reference-machine campaign still needs documented 1 GB and 10 GB
VALUE, skewed TOKEN, range, mixed fan-out, cold/warm latency, prediction error, peak
RSS, interruption/recovery, and storage-overhead results. This measurement work does
not reopen search or storage architecture and must not be replaced by small local
timings.

## Deferred beyond v1

JSON/Parquet import adapters, major-version migration tooling,
fuzzy names, entity resolution, ML/LLM features, concurrency, and a full-screen UI
remain out of scope. Pandoracle stays a CLI product with JSON for automation.
