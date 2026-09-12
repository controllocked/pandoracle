<p align="center">
  <img src="docs/assets/pandoracle-banner.svg" width="100%" alt="Pandoracle — private, offline, provenance-first dataset search">
</p>

<p align="center">
  <a href="https://github.com/controllocked/pandoracle/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/controllocked/pandoracle/ci.yml?branch=main&amp;style=flat-square&amp;label=CI"></a>
  <a href="https://www.python.org/"><img alt="Python 3.12+" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="Apache-2.0 license" src="https://img.shields.io/badge/License-Apache--2.0-6E56CF?style=flat-square"></a>
  <img alt="Linux" src="https://img.shields.io/badge/Platform-Linux-0F172A?style=flat-square&amp;logo=linux&amp;logoColor=white">
</p>

<p align="center">
  Import local CSV datasets, search them without sending records anywhere, and
  keep every result tied to its immutable source.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#pandora-removable-drives">Encrypted drives</a> ·
  <a href="#documentation">Documentation</a>
</p>

> [!IMPORTANT]
> Pandoracle is beta software and Linux-first. An ordinary workspace is not
> encrypted. For confidentiality at rest, use the optional Pandora removable-drive
> workflow or protect the containing filesystem independently.

## Why Pandoracle

- **Private by default.** Imports and searches run locally; there is no server,
  account, telemetry, or network search path.
- **Provenance built in.** Results contain complete original records and stable
  `RecordRef(dataset_version_id, record_ordinal)` references.
- **Immutable sources.** RAW input and published dataset versions are never edited
  in place.
- **Search without lock-in.** Every dataset remains searchable by scan. Exact-value
  and whole-token acceleration can be added, rebuilt, or removed independently.
- **Explicit schemas.** Inference is advisory. Every field must be confirmed, and
  `UNKNOWN` is a deliberate, valid choice.
- **Automation-friendly.** The CLI includes a clean, non-interactive JSON path with
  no prompts, branding, animation, or ANSI sequences.

Pandoracle is intentionally single-user and single-writer. It is designed for
careful local work, not as a shared search service.

## Quick start

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/guides/tools/) are required.
Install the current version directly from GitHub:

```console
uv tool install git+https://github.com/controllocked/pandoracle.git
```

Or install from a checkout:

```console
git clone https://github.com/controllocked/pandoracle.git
cd pandoracle
uv tool install .
```

Start Pandoracle and follow the workspace prompt:

```console
pandoracle
```

The direct workflow is equally short:

```console
pandoracle init
pandoracle import contacts.csv --dataset contacts
pandoracle acceleration configure contacts
pandoracle
```

Import opens an interactive schema review. Malformed rows can be aborted or
quarantined; automation must choose a policy explicitly when it does not want the
strict default.

`uv tool` owns the isolated environment, so users do not activate a virtualenv or
export a workspace variable. If uv reports that its tool directory is not in
`PATH`, run `uv tool update-shell` once and open a new terminal.

## Search and inspect

Inside the shell, enter a value for a quick automatic search. Pandoracle shows the
inferred semantic type and scope before searching. Shell commands are:

- `:search` — guided dataset, semantic type, operator, and multi-clue search;
- `:datasets` — show imported datasets and acceleration;
- `:types` — show built-in and custom semantic types;
- `:help` — repeat the explanation;
- `:quit` — leave the shell.

For dates, exact search requires a complete date. Use automatic or range search
for a year such as `2001` or a range such as `2001..2003`.

One-shot search is intended for scripts and non-sensitive terminal values:

```console
pandoracle search person@example.test --type EMAIL
pandoracle search \
  --clue 'PERSON_NAME:token=Vadim Li' \
  --clue 'DATE_OF_BIRTH:range=2007..2009' \
  --scan
pandoracle search person@example.test --format json
```

Without `--dataset`, search covers every active dataset. Multiple clues are
AND-combined. `--scan` permits a search that the planner estimates as expensive;
it does not force a scan.

Human-readable results include a stable record reference. Open the complete record
with:

```console
pandoracle inspect DATASET_VERSION_UUID:42
```

## Repeatable imports

For non-interactive imports, create and confirm a schema plan first:

```console
pandoracle schema analyze contacts.csv --output contacts.schema.json
pandoracle schema review contacts.schema.json
pandoracle import contacts.csv --dataset contacts --schema contacts.schema.json \
  --on-error abort --format json
```

Pre-release schema and acceleration plan files are not accepted by v1; regenerate
them with the corresponding `analyze` or `plan` command.

## Custom semantic types

Schema review can create a workspace custom exact-text type with an `EXACT` or
`TOKEN` automatic operator. Manage its lifecycle with:

```console
pandoracle types
pandoracle types retire customer_code
pandoracle types restore customer_code
```

Retiring a custom type hides it from new assignments. Existing datasets remain
searchable, and a schema revision may keep the type on the same column. Restore it
before assigning it to a new column or importing a new dataset that uses it.
Built-in types cannot be retired.

## Acceleration and maintenance

All datasets are searchable without acceleration. Configure only the lookups that
matter for repeated work:

```console
pandoracle acceleration plan contacts
pandoracle acceleration configure contacts
pandoracle acceleration show contacts
pandoracle acceleration rebuild contacts
pandoracle acceleration disable contacts
```

Maintenance commands are grouped away from the everyday workflow:

```console
pandoracle maintenance verify
pandoracle maintenance recover
pandoracle maintenance gc
pandoracle maintenance gc --apply
pandoracle maintenance operations
```

Recovery quarantines interrupted staging data. Garbage collection is a read-only
dry run unless `--apply` is present.

## Workspace selection

`pandoracle init [PATH]` creates and selects a workspace. `pandoracle workspace`
shows the saved selection, and `pandoracle workspace PATH` validates and selects an
existing workspace. Resolution order is:

1. command `--workspace`;
2. `PANDORACLE_WORKSPACE` for temporary overrides;
3. the ephemeral workspace of an active supervised Pandora device session;
4. the saved XDG selection.

The config is `$XDG_CONFIG_HOME/pandoracle/config.json` (normally
`~/.config/pandoracle/config.json`). The default workspace is
`$XDG_DATA_HOME/pandoracle/workspace` (normally
`~/.local/share/pandoracle/workspace`).

Use `pandoracle --help` and each command's `--help` for runnable examples.

## Pandora removable drives

The optional Pandora layer provisions and supervises a removable LUKS2 workspace
using standard Linux services. Create a dedicated drive interactively:

```console
pandoracle device setup
```

Setup shows the model, size, and device selected for erasure and requires exact
destructive confirmation. You choose the main passphrase. Pandoracle generates a
separate recovery credential, shows it once, and requires confirmation that it was
stored safely. Neither credential is persisted by Pandoracle.

The small `PANDORA_PUB` FAT area can contain an owner-supplied file hierarchy.
**Everything copied there is public and unencrypted.** The LUKS2/ext4 private area
remains separate.

Per-host controls are available without device paths or mount commands:

```console
pandoracle device list
pandoracle device settings --auto-open
pandoracle device settings --unlock automatic
pandoracle device public ~/pandora-public
pandoracle device close
pandoracle device forget
pandoracle device detection enable
```

Pandora support requires UDisks2, cryptsetup, pkexec/polkit, FreeDesktop Secret
Service, and a desktop that can launch `Terminal=true` entries. The Linux integration
package installs the narrow root-owned provisioning helper described in
[packaging/README.md](packaging/README.md). Without that package, destructive setup
fails closed. Read [SECURITY.md](SECURITY.md) before relying on the encrypted-storage
threat model.

## How it works

```text
CSV source ──► immutable RAW copy ──► validated Parquet records
                                            │
                                            ├──► canonical semantic values
                                            └──► optional, rebuildable indexes

                               SQLite catalog publishes the version last
```

The catalog stores metadata rather than record-scale postings. Filesystem artifacts
are validated, synced, and moved into final paths before one catalog transaction
switches the active dataset version. This keeps stable record references independent
of rebuildable Parquet coordinates and SQLite implementation details.

## Documentation

| Guide | What it covers |
| --- | --- |
| [Architecture](docs/ARCHITECTURE.md) | Components, invariants, and data flow |
| [Database](docs/DATABASE.md) | Catalog schema, publication, recovery, and migrations |
| [CLI UX](docs/CLI_UX.md) | Interactive and non-interactive behavior |
| [Security](SECURITY.md) | Threat model, privilege boundaries, and device credentials |
| [Benchmarks](docs/BENCHMARKS.md) | Reproducible performance methodology and reports |
| [Roadmap](docs/ROADMAP.md) | Current scope and remaining milestones |
| [ADRs](docs/adr/) | Architectural decisions and their rationale |

## Development

```console
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,analytics]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/pytest --cov=pandoracle
```

Benchmark helpers live under `scripts/`; they are not part of the installed CLI.
Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## License

Pandoracle is available under the [Apache License 2.0](LICENSE).
