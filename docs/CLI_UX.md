# CLI UX conventions

Pandoracle has one public human interface: the `pandoracle` command. The default
interactive experience is the shell; ordinary subcommands remain predictable for
scripts. A full-screen TUI is out of scope.

## Entry and workspace selection

- Bare `pandoracle` in a TTY shows branding once. It opens the selected non-empty
  workspace in the shell, or guides first-time workspace setup.
- Bare `pandoracle` without a TTY prints short help and never prompts.
- `pandoracle shell` also shows branding once in a TTY. Branding never appears on
  redirected output, ordinary subcommands, or JSON output.
- Full branding is centered at 60 columns or wider; terminals from 42 to 59
  columns receive the centered compact logo, and narrower terminals receive the
  plain-text fallback. The `hunt through private data` slogan follows the brand,
  separated from shell output by a blank line. Brand assets must fit these
  thresholds without wrapping.
- Workspace precedence is command `--workspace`, then `PANDORACLE_WORKSPACE`, then
  the active device session, then the atomic XDG config selection.
- `init` creates and selects; `workspace PATH` validates and selects. Users do not
  manage a Python virtualenv or an environment variable during normal use.

## Public command surface

```text
init                         workspace
import                       datasets
inspect                      search
shell                        types
types retire|restore         schema analyze|review|revise
acceleration show|plan|configure|rebuild|disable
maintenance verify|recover|gc|operations
device setup|trust|forget|open|close|settings|public|list|detection
```

There are no pre-v1 aliases. Developer data generation and benchmarks belong in
`scripts/`, not in the installed CLI.

## Pandora removable devices

`device setup` is interactive and requires exact whole-drive erase confirmation,
two no-echo entries of a non-empty main passphrase, and acknowledgement of the
one-time recovery credential. Automatic unlock is off unless explicitly requested.
Its warning says that a separate host key is stored in Secret Service and is enough
to unlock the device from that login.

Setup asks for an optional local public-content directory. It clearly warns that
every file below that directory is copied to `PANDORA_PUB` and remains public and
unencrypted. An empty answer creates no user-visible public files. The same hierarchy
can later be replaced as a unit with `device public DIRECTORY`; Pandoracle assigns no
meaning to those files. Symbolic links and special files are rejected because the
FAT32 destination cannot preserve them.

After erase confirmation and passphrase collection, setup requests administrator
authorization once for the complete destructive provisioning transaction. The
root-owned packaged helper has a dedicated polkit action; setup never offers a broad
passwordless rule and never elevates the client, venv, or checkout. A missing or
unsafe helper stops with package-installation guidance instead of falling back to
multiple authorization dialogs.

Insertion detection is a per-user XDG autostart watcher. Per-device settings control
auto-open and prompt/automatic unlock. Auto-open creates a dedicated terminal; the
device supervisor owns its shell process group. `:quit` and Ctrl-D lead to flush,
non-forced unmount, lock, and terminal exit. Physical removal terminates that shell
and closes only the dedicated terminal. A manually invoked `device open` returns to
the existing terminal. While this device session is open, ordinary commands run in
another terminal (`pandoracle import`, `pandoracle datasets`, `pandoracle search`,
and the rest) automatically use its workspace without `--workspace`. On a busy clean
close, the dedicated terminal reports same-user holder processes where possible and
retries automatically without force; physical removal still ends the supervisor.

Device selection, setup decisions, destructive actions, and credential entry use the
same interactive prompt style as guided shell search wherever the input permits it.
Mount commands, encryption commands, environment variables, and Python environments
are not part of the ordinary device UX.

## Interactive shell

Bare input performs quick AUTO search across all applicable active datasets and
prints the inferred type and resolved operator. `:search` runs the guided dataset,
type, operator, and multi-clue flow. `:datasets`, `:types`, `:help`, and `:quit`
provide discovery without leaving the shell. On entry, the shell prints only its
name and points to `:help`; the detailed instructions are shown on request.

The operator menu is derived from the semantic contract. Date choices name their
accepted input forms and validate before advancing, so a birth year cannot be
accepted as an exact date. Candidate pagination, refinement, open, and back state
exist only in process memory. Entered query values are not written to application
history, though terminal capture, swap, and a compromised host remain outside that
guarantee.

## Schema and semantic types

Inference is advisory. Review shows the whole proposal before prompting, every
field needs an explicit decision, and `UNKNOWN` is valid. Samples are read from the
source and are never stored in the plan or catalog.

Built-in semantic types are fixed. Workspace custom types can be active or retired.
Retired types do not appear as choices for new assignments. An existing schema
revision may retain its retired type on the same field, but may not move it to a new
field; new import plans using it fail with `pandoracle types restore TYPE` guidance.

## Help, output, and errors

- Root help leads with `pandoracle`, `init`, `import`, and `search`.
- Every public command/group explains purpose, important defaults and consequences,
  and includes a runnable example.
- Human output says “exact-value lookup” and “whole-token lookup”. `VALUE` and
  `TOKEN` remain wire identifiers in JSON only.
- Search tables expose record references and direct the user to `inspect`; JSON
  contains complete records and provenance.
- Expected errors are concise and name the next valid command. Tracebacks appear
  only with `PANDORACLE_DEBUG=1`.

## Automation

`search --format json` writes one JSON document to stdout. It does not prompt, show
branding, render progress, or emit ANSI control characters. Non-interactive import
and acceleration configuration require confirmed v1 plan files. Public v1 JSON
envelopes and plans use version marker `1`; pre-release formats are rejected rather
than guessed or upgraded.

## Progress and destructive actions

Progress is TTY-only stderr output and uses a single live task. Percentages appear
only with an exact denominator. Garbage collection is a non-mutating dry run by
default and requires `--apply`; it re-plans under the exclusive lock. Recovery moves
staging data to quarantine and does not delete published data.
