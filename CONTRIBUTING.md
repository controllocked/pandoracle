# Contributing

Thanks for helping improve Pandoracle. Bug reports, focused fixes, documentation,
and reproducible performance work are welcome.

## Ground rules

- Keep public code, CLI text, tests, ADRs, and primary documentation English-first.
- Use only deterministic synthetic or clearly redistributable data in the
  repository, issues, and benchmarks.
- Keep user-facing errors concise; unexpected tracebacks are available only with
  `PANDORACLE_DEBUG=1`.
- Open an issue before making an architectural change or adding a production
  dependency.

Security reports do not belong in public issues. Follow the private reporting
instructions in [SECURITY.md](SECURITY.md).

## Development

Create an isolated environment and install every development extra:

```console
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,analytics]'
```

Keep changes narrow, add or update the closest focused test, and document visible
behavior. Changes to workspace identity, publication ordering, provenance, on-disk
formats, threat-model claims, or privilege boundaries require an ADR.

## Checks

Before submitting a change:

```console
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/pytest --cov=pandoracle
```

Before publishing a release artifact:

```console
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

Small local measurements are useful for iteration but are not release benchmark
evidence. See [docs/BENCHMARKS.md](docs/BENCHMARKS.md) for the reporting contract.
