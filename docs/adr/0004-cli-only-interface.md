# ADR 0004: CLI-only interface and search boundary

Status: accepted

## Context

Pandoracle needs efficient interactive confirmation and readable search output,
but a full-screen interface would create a second navigation and automation
model. The search engine also needs a clear stopping point: finding a record is
not authorization to act on it through another system.

## Decision

The supported human interface is a conventional CLI. Inline arrow-key prompts,
tables, and progress displays are allowed; a full-screen TUI is not planned.
Every interactive workflow must retain an explicit non-interactive path.

Search without a dataset filter covers all active dataset versions through
semantic indexes. A hit resolves to the complete source record, a stable
`RecordRef`, and provenance. Downstream use of the record remains the user's
responsibility outside the core search engine.

The core describes encrypted storage only as an external deployment property and
does not provide encryption or credential management. The separate CLI device layer
accepted in ADR 0008 may create and supervise a mounted encrypted filesystem before
passing its ordinary workspace path to that core.

## Consequences

- CLI interaction conventions are maintained in `docs/CLI_UX.md`.
- JSON output remains the automation interface and is never mixed with prompts
  or progress rendering.
- Product planning does not include a Textual or other full-screen TUI.
- Integrations that take action on a search result must remain outside the core
  and require their own explicit authorization and threat model.
