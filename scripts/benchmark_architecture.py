#!/usr/bin/env python3
"""Generate and exercise the reproducible architecture benchmark corpus.

This is a developer benchmark driver, not an acceptance-test shortcut.  Its JSON
outputs are intended to be combined with externally captured wall time and RSS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import sqlite3
import statistics
import subprocess
import sys
import time
from importlib.metadata import version
from math import ceil
from pathlib import Path
from typing import Any

from pandoracle import __version__
from pandoracle.acceleration_profiles import (
    configure_acceleration,
    confirm_acceleration_plan,
    make_acceleration_plan,
)
from pandoracle.ingest import analyze_csv, import_csv
from pandoracle.models import AccelerationKind, SearchClue, SearchOperator, SearchRequest
from pandoracle.schema import confirm_plan
from pandoracle.search import search
from pandoracle.workspace import Workspace

SEED = 20260912
FIELDS = (
    "full_name",
    "email",
    "date_of_birth",
    "phone",
    "city",
    "ip_address",
    "payload",
)
SEMANTIC_TYPES = {
    0: "PERSON_NAME",
    1: "EMAIL",
    2: "DATE_OF_BIRTH",
    3: "PHONE",
    4: "UNKNOWN",
    5: "IP",
    6: "UNKNOWN",
}


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _latency_summary(values: list[float]) -> dict[str, float]:
    return {
        "min": min(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _tree_file_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _payload(seed: int, ordinal: int, size: int) -> str:
    digest = hashlib.blake2b(f"{seed}:{ordinal}".encode(), digest_size=64).hexdigest()
    return (digest * ceil(size / len(digest)))[:size]


def _name(local_ordinal: int, global_ordinal: int, rare_local: int) -> str:
    if local_ordinal == rare_local:
        return "Zephyr Benchmark"
    remainder = global_ordinal % 16
    if remainder < 8:
        return "Alexey Ivanov"
    if remainder < 11:
        return "Maria Smith"
    if remainder < 13:
        return "Daria Muller"
    if remainder < 15:
        return "Ivan Petrov"
    return "Oleksandr Kovalenko"


def generate(args: argparse.Namespace) -> None:
    if args.shards < 1 or args.rows_per_shard < 1 or args.payload_bytes < 0:
        raise SystemExit("shards and rows must be positive; payload bytes cannot be negative")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    rare_local = min(424_243, args.rows_per_shard - 1)
    files: list[dict[str, Any]] = []
    total_bytes = 0
    started = time.perf_counter()
    for shard in range(args.shards):
        path = output / f"benchmark-{shard:02d}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(",".join(FIELDS) + "\n")
            for local in range(args.rows_per_shard):
                ordinal = shard * args.rows_per_shard + local
                name = _name(local, ordinal, rare_local)
                email = (
                    "shared.benchmark@example.test"
                    if local % 100_000 == 0
                    else f"person{ordinal:09d}@example.test"
                )
                year = 1970 + ((ordinal * 17 + args.seed) % 40)
                month = 1 + ((ordinal * 7 + shard) % 12)
                day = 1 + ((ordinal * 11 + shard) % 28)
                phone = f"+1555{ordinal % 10_000_000:07d}"
                city = ("Almaty", "Berlin", "Kyiv", "London", "Warsaw")[ordinal % 5]
                ip = f"10.{(ordinal // 65_536) % 256}.{(ordinal // 256) % 256}.{ordinal % 256}"
                row = (
                    name,
                    email,
                    f"{year:04d}-{month:02d}-{day:02d}",
                    phone,
                    city,
                    ip,
                    _payload(args.seed, ordinal, args.payload_bytes),
                )
                stream.write(",".join(row) + "\n")
        size = path.stat().st_size
        total_bytes += size
        files.append(
            {
                "dataset": f"bench-{shard:02d}",
                "path": str(path),
                "rows": args.rows_per_shard,
                "bytes": size,
                "sha256": _file_sha256(path),
            }
        )
    manifest = {
        "benchmark_corpus_version": 1,
        "seed": args.seed,
        "shards": args.shards,
        "rows_per_shard": args.rows_per_shard,
        "rows": args.shards * args.rows_per_shard,
        "payload_bytes": args.payload_bytes,
        "raw_bytes": total_bytes,
        "files": files,
        "distribution": {
            "email": "unique except one shared sentinel every 100000 local rows",
            "person_name": "50% Alexey; one Zephyr Benchmark row per shard",
            "date_of_birth": "deterministic 40-year cycle from 1970 through 2009",
            "payload": "deterministic synthetic hexadecimal filler; semantically UNKNOWN",
        },
        "queries": {
            "exact_present": "person000000123@example.test",
            "exact_absent": "absent.benchmark@example.test",
            "exact_frequent": "shared.benchmark@example.test",
            "token_frequent": "Alexey",
            "token_rare": "Zephyr",
            "token_pair": "Zephyr Benchmark",
            "range_year": "1985",
            "range_multi": "1985..1989",
            "mixed_name": _name(123, 123, rare_local).split()[0],
            "mixed_year": str(1970 + ((123 * 17 + args.seed) % 40)),
        },
        "generation_seconds": time.perf_counter() - started,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))


def init_workspace(args: argparse.Namespace) -> None:
    workspace = Workspace.create(args.workspace)
    print(
        json.dumps(
            {"workspace": str(workspace.root), "workspace_id": workspace.manifest.workspace_id}
        )
    )


def import_shard(args: argparse.Namespace) -> None:
    workspace = Workspace.open(args.workspace)
    draft = analyze_csv(args.source)
    plan = confirm_plan(draft, SEMANTIC_TYPES, selection_source="benchmark_manifest")
    phases: list[dict[str, Any]] = []
    last_phase: str | None = None
    phase_started = time.perf_counter()
    temporary_high_water_bytes = 0

    def progress(item: Any) -> None:
        nonlocal last_phase, phase_started, temporary_high_water_bytes
        temporary_high_water_bytes = max(
            temporary_high_water_bytes,
            _tree_file_bytes(workspace.root / "operations/staging"),
        )
        phase = item.phase.value
        now = time.perf_counter()
        if last_phase is not None and phase != last_phase:
            phases.append({"phase": last_phase, "seconds": now - phase_started})
            phase_started = now
        last_phase = phase

    started = time.perf_counter()
    result = import_csv(
        workspace,
        args.source,
        dataset_name=args.dataset,
        schema_plan=plan,
        progress=progress,
    )
    ended = time.perf_counter()
    if last_phase is not None:
        phases.append({"phase": last_phase, "seconds": ended - phase_started})
    print(
        json.dumps(
            {
                "operation": "import",
                "wall_seconds": ended - started,
                "rows_per_second": result.row_count / max(ended - started, 1e-9),
                "phases": phases,
                "temporary_high_water_bytes": temporary_high_water_bytes,
                "result": result.to_dict(),
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            },
            indent=2,
            sort_keys=True,
        )
    )


def configure_profile(args: argparse.Namespace) -> None:
    workspace = Workspace.open(args.workspace)
    plan = make_acceleration_plan(workspace, args.dataset)
    choices: dict[int, tuple[AccelerationKind, ...]] = {}
    for field in plan.fields:
        if (
            field.semantic_type in {"EMAIL", "DATE_OF_BIRTH"}
            and args.profile in {"value", "both"}
        ):
            choices[field.field_id] = (AccelerationKind.VALUE,)
        elif field.semantic_type == "PERSON_NAME" and args.profile in {"token", "both"}:
            choices[field.field_id] = (AccelerationKind.TOKEN,)
        else:
            choices[field.field_id] = ()
    confirmed = confirm_acceleration_plan(plan, choices)
    started = time.perf_counter()
    result = configure_acceleration(workspace, args.dataset, confirmed, force=args.force)
    elapsed = time.perf_counter() - started
    selected_estimates = [
        item.to_dict()
        for item in plan.estimates
        if item.primitive in choices.get(item.field_id, ())
    ]
    print(
        json.dumps(
            {
                "operation": "configure_acceleration",
                "profile": args.profile,
                "wall_seconds": elapsed,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "plan": confirmed.to_dict(),
                "selected_estimates": selected_estimates,
                "result": result.to_dict(),
            },
            indent=2,
            sort_keys=True,
        )
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _evict_page_cache(paths: list[Path]) -> None:
    """Best-effort per-file eviction; this is not equivalent to a reboot."""
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(descriptor)


def _requests(manifest: dict[str, Any], dataset: str | None) -> dict[str, SearchRequest]:
    query = manifest["queries"]
    common = {"dataset": dataset, "limit": 50, "allow_expensive_scan": True}
    mixed_clues = (
        SearchClue("EMAIL", query["exact_present"], SearchOperator.EXACT),
        SearchClue("PERSON_NAME", query["mixed_name"], SearchOperator.TOKEN),
        SearchClue("DATE_OF_BIRTH", query["mixed_year"], SearchOperator.RANGE),
    )
    return {
        "exact_present": SearchRequest(
            (SearchClue("EMAIL", query["exact_present"], SearchOperator.EXACT),), **common
        ),
        "exact_absent": SearchRequest(
            (SearchClue("EMAIL", query["exact_absent"], SearchOperator.EXACT),), **common
        ),
        "exact_frequent": SearchRequest(
            (SearchClue("EMAIL", query["exact_frequent"], SearchOperator.EXACT),), **common
        ),
        "token_frequent": SearchRequest(
            (SearchClue("PERSON_NAME", query["token_frequent"], SearchOperator.TOKEN),),
            **common,
        ),
        "token_rare": SearchRequest(
            (SearchClue("PERSON_NAME", query["token_rare"], SearchOperator.TOKEN),), **common
        ),
        "token_pair": SearchRequest(
            (SearchClue("PERSON_NAME", query["token_pair"], SearchOperator.TOKEN),), **common
        ),
        "range_year": SearchRequest(
            (SearchClue("DATE_OF_BIRTH", query["range_year"], SearchOperator.RANGE),), **common
        ),
        "range_multi": SearchRequest(
            (SearchClue("DATE_OF_BIRTH", query["range_multi"], SearchOperator.RANGE),), **common
        ),
        "mixed": SearchRequest(mixed_clues, **common),
        "mixed_reversed": SearchRequest(tuple(reversed(mixed_clues)), **common),
    }


def pandoracle_search(args: argparse.Namespace) -> None:
    workspace = Workspace.open(args.workspace)
    manifest = _load_manifest(args.manifest)
    requests = _requests(manifest, None if args.scope == "fanout" else args.dataset)
    selected = args.workload or list(requests)
    report: dict[str, Any] = {}
    cache_paths = [path for path in workspace.root.rglob("*") if path.is_file()]
    for name in selected:
        request = requests[name]
        for _ in range(args.warmup):
            search(workspace, request)
        latencies: list[float] = []
        result = None
        for _ in range(args.iterations):
            if args.advised_cold:
                _evict_page_cache(cache_paths)
            started = time.perf_counter_ns()
            result = search(workspace, request)
            latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        assert result is not None
        report[name] = {
            "latency_ms": _latency_summary(latencies),
            "returned_records": len(result.records),
            "truncated": result.truncated,
            "execution": result.execution,
            "record_refs": [item.ref.external_id for item in result.records],
        }
    print(
        json.dumps(
            {
                "engine": "pandoracle",
                "scope": args.scope,
                "dataset": None if args.scope == "fanout" else args.dataset,
                "cache_state": (
                    "POSIX_FADV_DONTNEED before each trial; advisory, not reboot-cold"
                    if args.advised_cold
                    else "warm-process; OS page cache not controlled"
                ),
                "warmup": args.warmup,
                "iterations": args.iterations,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "workloads": report,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _read_peak_rss(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0


def _run_command(command: list[str]) -> tuple[float, int, int]:
    started = time.perf_counter_ns()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    peak_rss = 0
    while process.poll() is None:
        peak_rss = max(peak_rss, _read_peak_rss(process.pid))
        time.sleep(0.001)
    stdout, stderr = process.communicate()
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if process.returncode not in {0, 1}:
        message = stderr.decode(errors="replace")
        raise RuntimeError(f"command failed ({process.returncode}): {message}")
    return elapsed_ms, peak_rss, len(stdout.splitlines())


def external_search(args: argparse.Namespace) -> None:
    manifest = _load_manifest(args.manifest)
    paths = [Path(item["path"]) for item in manifest["files"]]
    if args.scope == "single":
        paths = paths[:1]
    query = manifest["queries"]
    programs = {
        "exact_present": ("fixed", f",{query['exact_present']},"),
        "exact_absent": ("fixed", f",{query['exact_absent']},"),
        "exact_frequent": ("fixed", f",{query['exact_frequent']},"),
        "token_frequent": ("regex", r"(^| )Alexey( |,)"),
        "token_rare": ("regex", r"(^| )Zephyr( |,)"),
        "token_pair": ("regex", r"(^| )Zephyr Benchmark( |,)"),
        "range_year": ("awk", f'$3 ~ /^{query["range_year"]}-/'),
        "range_multi": (
            "awk",
            f'$3 >= "{query["range_multi"].split("..")[0]}-01-01" '
            f'&& $3 <= "{query["range_multi"].split("..")[1]}-12-31"',
        ),
        "mixed": (
            "awk",
            f'$2 == "{query["exact_present"]}" '
            f'&& $1 ~ /(^| ){query["mixed_name"]}( |$)/ '
            f'&& $3 ~ /^{query["mixed_year"]}-/',
        ),
    }
    selected = args.workload or list(programs)
    report: dict[str, Any] = {}
    for name in selected:
        mode, pattern = programs[name]
        if args.engine == "awk":
            if mode != "awk":
                continue
            program = f"{pattern} {{print; found++; if (found >= 51) exit}}"
            command = [args.binary, "-F,", program]
        elif mode == "awk":
            continue
        elif args.engine == "grep":
            command = [
                args.binary,
                "-h",
                "-m",
                "51",
                "-F" if mode == "fixed" else "-E",
                pattern,
            ]
        else:
            command = [
                args.binary,
                "--no-heading",
                "-m",
                "51",
                "-F" if mode == "fixed" else "-e",
                pattern,
            ]
        command.extend(str(path) for path in paths)
        for _ in range(args.warmup):
            _run_command(command)
        latencies: list[float] = []
        rss_values: list[int] = []
        line_count = 0
        for _ in range(args.iterations):
            if args.advised_cold:
                _evict_page_cache(paths)
            elapsed, rss, line_count = _run_command(command)
            latencies.append(elapsed)
            rss_values.append(rss)
        report[name] = {
            "latency_ms": _latency_summary(latencies),
            "peak_rss_kib": max(rss_values),
            "returned_lines": line_count,
        }
    print(
        json.dumps(
            {
                "engine": args.engine,
                "binary": args.binary,
                "scope": args.scope,
                "cache_state": (
                    "POSIX_FADV_DONTNEED before each trial; advisory, not reboot-cold"
                    if args.advised_cold
                    else "warm OS page cache; fresh process per iteration"
                ),
                "warmup": args.warmup,
                "iterations": args.iterations,
                "contract": "raw matching CSV lines; no parsing, normalization, or provenance",
                "workloads": report,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _duckdb_source(paths: list[Path]) -> str:
    values = ",".join(_sql_literal(str(path)) for path in paths)
    argument = f"[{values}]" if len(paths) > 1 else values
    return f"read_csv({argument}, header=true, all_varchar=true)"


def duckdb_prepare(args: argparse.Namespace) -> None:
    import duckdb

    manifest = _load_manifest(args.manifest)
    connection = duckdb.connect(str(args.database))
    started = time.perf_counter()
    connection.execute("DROP TABLE IF EXISTS records")
    first = True
    for item in manifest["files"]:
        source = _duckdb_source([Path(item["path"])])
        statement = (
            "CREATE TABLE records AS " if first else "INSERT INTO records "
        ) + f"SELECT {_sql_literal(item['dataset'])} AS dataset, * FROM {source}"
        connection.execute(statement)
        first = False
    load_seconds = time.perf_counter() - started
    index_started = time.perf_counter()
    connection.execute("CREATE INDEX email_index ON records(email)")
    connection.execute("CREATE INDEX dob_index ON records(date_of_birth)")
    connection.execute("ANALYZE records")
    index_seconds = time.perf_counter() - index_started
    row_count = connection.execute("SELECT count(*) FROM records").fetchone()[0]
    connection.close()
    print(
        json.dumps(
            {
                "engine": "duckdb-materialized",
                "database": str(args.database.resolve()),
                "row_count": row_count,
                "load_seconds": load_seconds,
                "index_seconds": index_seconds,
                "wall_seconds": time.perf_counter() - started,
                "database_bytes": args.database.stat().st_size,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            },
            indent=2,
            sort_keys=True,
        )
    )


def duckdb_search(args: argparse.Namespace) -> None:
    import duckdb

    manifest = _load_manifest(args.manifest)
    query = manifest["queries"]
    paths = [Path(item["path"]) for item in manifest["files"]]
    if args.scope == "single":
        paths = paths[:1]
    if args.mode == "csv":
        source = _duckdb_source(paths)
        scope_filter = ""
        connection = duckdb.connect(":memory:")
    else:
        source = "records"
        scope_filter = "dataset = 'bench-00' AND " if args.scope == "single" else ""
        connection = duckdb.connect(str(args.database), read_only=True)
    conditions = {
        "exact_present": f"email = {_sql_literal(query['exact_present'])}",
        "exact_absent": f"email = {_sql_literal(query['exact_absent'])}",
        "exact_frequent": f"email = {_sql_literal(query['exact_frequent'])}",
        "token_frequent": "regexp_matches(full_name, '(^| )Alexey( |$)')",
        "token_rare": "regexp_matches(full_name, '(^| )Zephyr( |$)')",
        "token_pair": "regexp_matches(full_name, '(^| )Zephyr Benchmark( |$)')",
        "range_year": f"date_of_birth BETWEEN '{query['range_year']}-01-01' "
        f"AND '{query['range_year']}-12-31'",
        "range_multi": (
            f"date_of_birth BETWEEN '{query['range_multi'].split('..')[0]}-01-01' "
            f"AND '{query['range_multi'].split('..')[1]}-12-31'"
        ),
        "mixed": (
            f"email = {_sql_literal(query['exact_present'])} "
            f"AND regexp_matches(full_name, '(^| ){query['mixed_name']}( |$)') "
            f"AND date_of_birth BETWEEN '{query['mixed_year']}-01-01' "
            f"AND '{query['mixed_year']}-12-31'"
        ),
    }
    selected = args.workload or list(conditions)
    report: dict[str, Any] = {}
    for name in selected:
        statement = f"SELECT * FROM {source} WHERE {scope_filter}{conditions[name]} LIMIT 51"
        for _ in range(args.warmup):
            connection.execute(statement).fetchall()
        latencies: list[float] = []
        rows: list[tuple[Any, ...]] = []
        for _ in range(args.iterations):
            started = time.perf_counter_ns()
            rows = connection.execute(statement).fetchall()
            latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        report[name] = {
            "latency_ms": _latency_summary(latencies),
            "returned_records": len(rows),
        }
    connection.close()
    print(
        json.dumps(
            {
                "engine": f"duckdb-{args.mode}",
                "scope": args.scope,
                "cache_state": "warm-process; OS page cache not controlled",
                "warmup": args.warmup,
                "iterations": args.iterations,
                "contract": "parsed complete records; no semantic normalization or provenance",
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "workloads": report,
            },
            indent=2,
            sort_keys=True,
        )
    )


def ground_truth(args: argparse.Namespace) -> None:
    import duckdb

    manifest = _load_manifest(args.manifest)
    query = manifest["queries"]
    conditions = {
        "exact_present": f"email = {_sql_literal(query['exact_present'])}",
        "exact_absent": f"email = {_sql_literal(query['exact_absent'])}",
        "exact_frequent": f"email = {_sql_literal(query['exact_frequent'])}",
        "token_frequent": "regexp_matches(full_name, '(^| )Alexey( |$)')",
        "token_rare": "regexp_matches(full_name, '(^| )Zephyr( |$)')",
        "token_pair": "regexp_matches(full_name, '(^| )Zephyr Benchmark( |$)')",
        "range_year": f"date_of_birth BETWEEN '{query['range_year']}-01-01' "
        f"AND '{query['range_year']}-12-31'",
        "range_multi": (
            f"date_of_birth BETWEEN '{query['range_multi'].split('..')[0]}-01-01' "
            f"AND '{query['range_multi'].split('..')[1]}-12-31'"
        ),
        "mixed": (
            f"email = {_sql_literal(query['exact_present'])} "
            f"AND regexp_matches(full_name, '(^| ){query['mixed_name']}( |$)') "
            f"AND date_of_birth BETWEEN '{query['mixed_year']}-01-01' "
            f"AND '{query['mixed_year']}-12-31'"
        ),
    }
    connection = duckdb.connect(str(args.database), read_only=True)
    result: dict[str, Any] = {}
    for name, condition in conditions.items():
        total = connection.execute(f"SELECT count(*) FROM records WHERE {condition}").fetchone()[0]
        by_dataset = dict(
            connection.execute(
                "SELECT dataset,count(*) FROM records "
                f"WHERE {condition} GROUP BY dataset ORDER BY dataset"
            ).fetchall()
        )
        result[name] = {"all": total, "by_dataset": by_dataset}
    connection.close()
    print(json.dumps({"source": "DuckDB materialized corpus", "counts": result}, indent=2))


def environment(args: argparse.Namespace) -> None:
    values: dict[str, Any] = {
        "pandoracle": __version__,
        "python": sys.version.split()[0],
        "pyarrow": version("pyarrow"),
        "sqlite": sqlite3.sqlite_version,
        "duckdb": version("duckdb"),
        "kernel": platform.release(),
        "distribution": platform.freedesktop_os_release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "cpu_model": next(
            (
                line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")
            ),
            "unknown",
        ),
        "memory_kib": next(
            int(line.split()[1])
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemTotal:")
        ),
        "note": "Development VM metadata; storage details are captured by the campaign commands.",
    }
    print(json.dumps(values, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("output", type=Path)
    generate_parser.add_argument("--shards", type=int, default=4)
    generate_parser.add_argument("--rows-per-shard", type=int, default=540_000)
    generate_parser.add_argument("--payload-bytes", type=int, default=384)
    generate_parser.add_argument("--seed", type=int, default=SEED)
    generate_parser.set_defaults(handler=generate)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("workspace", type=Path)
    init_parser.set_defaults(handler=init_workspace)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("workspace", type=Path)
    import_parser.add_argument("source", type=Path)
    import_parser.add_argument("dataset")
    import_parser.set_defaults(handler=import_shard)

    profile_parser = subparsers.add_parser("profile")
    profile_parser.add_argument("workspace", type=Path)
    profile_parser.add_argument("dataset")
    profile_parser.add_argument("profile", choices=("none", "value", "token", "both"))
    profile_parser.add_argument("--force", action="store_true")
    profile_parser.set_defaults(handler=configure_profile)

    search_parser = subparsers.add_parser("pandoracle-search")
    search_parser.add_argument("workspace", type=Path)
    search_parser.add_argument("manifest", type=Path)
    search_parser.add_argument("--scope", choices=("single", "fanout"), default="single")
    search_parser.add_argument("--dataset", default="bench-00")
    search_parser.add_argument("--workload", action="append")
    search_parser.add_argument("--warmup", type=int, default=3)
    search_parser.add_argument("--iterations", type=int, default=30)
    search_parser.add_argument("--advised-cold", action="store_true")
    search_parser.set_defaults(handler=pandoracle_search)

    external_parser = subparsers.add_parser("external-search")
    external_parser.add_argument("manifest", type=Path)
    external_parser.add_argument("engine", choices=("grep", "ripgrep", "awk"))
    external_parser.add_argument("binary")
    external_parser.add_argument("--scope", choices=("single", "fanout"), default="single")
    external_parser.add_argument("--workload", action="append")
    external_parser.add_argument("--warmup", type=int, default=3)
    external_parser.add_argument("--iterations", type=int, default=30)
    external_parser.add_argument("--advised-cold", action="store_true")
    external_parser.set_defaults(handler=external_search)

    duckdb_prepare_parser = subparsers.add_parser("duckdb-prepare")
    duckdb_prepare_parser.add_argument("manifest", type=Path)
    duckdb_prepare_parser.add_argument("database", type=Path)
    duckdb_prepare_parser.set_defaults(handler=duckdb_prepare)

    duckdb_search_parser = subparsers.add_parser("duckdb-search")
    duckdb_search_parser.add_argument("manifest", type=Path)
    duckdb_search_parser.add_argument("mode", choices=("csv", "table"))
    duckdb_search_parser.add_argument("--database", type=Path)
    duckdb_search_parser.add_argument("--scope", choices=("single", "fanout"), default="single")
    duckdb_search_parser.add_argument("--workload", action="append")
    duckdb_search_parser.add_argument("--warmup", type=int, default=3)
    duckdb_search_parser.add_argument("--iterations", type=int, default=30)
    duckdb_search_parser.set_defaults(handler=duckdb_search)

    ground_truth_parser = subparsers.add_parser("ground-truth")
    ground_truth_parser.add_argument("manifest", type=Path)
    ground_truth_parser.add_argument("database", type=Path)
    ground_truth_parser.set_defaults(handler=ground_truth)

    environment_parser = subparsers.add_parser("environment")
    environment_parser.set_defaults(handler=environment)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
