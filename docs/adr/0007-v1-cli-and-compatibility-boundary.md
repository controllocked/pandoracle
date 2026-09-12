# ADR 0007: v1 CLI and compatibility boundary

- Status: Accepted
- Date: 2026-09-07

## Context

The pre-release CLI required users to activate a project environment, repeatedly
pass or export a workspace path, understand internal profile terms, and navigate
several obsolete command names. Custom semantic types had no lifecycle. Compatibility
code for experimental catalogs, JSON documents, and EXACT indexes increased the
surface before a public release existed.

## Decision

`pandoracle` is the primary interactive entry point. It is installed as an isolated
uv tool, persists the selected workspace atomically in the XDG config directory, and
uses an XDG data default. CLI workspace resolution is explicit option, environment
override, then saved selection; the core still receives an ordinary POSIX path.

The public command tree is the one documented in `CLI_UX.md`. Bare interactive
startup and explicit shell startup show packaged ASCII branding once. Redirected,
JSON, and ordinary command output do not.

Workspace custom semantic types have active and retired states. Retirement blocks
new assignments while preserving existing typed search and same-field retention
during revision. Built-in types are immutable.

Public application and JSON contracts begin at 1.0.0/version 1. Pre-release aliases,
fallback parsers, preference models, EXACT-index runtime paths, and catalog v1-v3
migrations are removed. Catalog v4→v5 is the only supported development migration;
it is additive and backup-first. Workspaces containing pre-v1 EXACT artifacts must
be recreated.

## Consequences

New users need no implementation knowledge, virtualenv activation, or persistent env
setup. Automation retains an unbranded, non-interactive JSON path. The codebase has a
smaller public and compatibility surface. Pre-release workspaces other than clean v4
and pre-release plan files are intentionally disposable.
