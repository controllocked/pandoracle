#!/usr/bin/env python3
"""Aggregate import profile JSON files without reading benchmark source data."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _timing(report: dict[str, Any], name: str) -> float:
    return float(report["timings"].get(name, {}).get("wall_s", 0.0))


def _profile_function(report: dict[str, Any], function: str) -> dict[str, Any] | None:
    return next(
        (item for item in report.get("cprofile_top", []) if item["function"] == function),
        None,
    )


def _run_summary(report: dict[str, Any]) -> dict[str, Any]:
    total = float(report["import"]["wall_s"])
    intervals = report["intervals"]
    first = [float(item["rows_per_s"]) for item in intervals[:5]]
    last = [float(item["rows_per_s"]) for item in intervals[-5:]]
    first_median = statistics.median(first) if first else 0.0
    last_median = statistics.median(last) if last else 0.0
    decline = (
        (first_median - last_median) / first_median * 100 if first_median else 0.0
    )
    index_first = [float(item["wall_s"]) for item in report["index_batches"][:5]]
    index_last = [float(item["wall_s"]) for item in report["index_batches"][-5:]]
    return {
        "rows": report["rows"],
        "total_wall_s": total,
        "process_cpu_s": report["import"]["process_cpu_s"],
        "cpu_to_wall": float(report["import"]["process_cpu_s"]) / total,
        "rows_per_s": int(report["rows"]) / total,
        "maxrss_kib": report["import"]["maxrss_kib"],
        "read_bytes": report["import"]["read_bytes"],
        "write_bytes": report["import"]["write_bytes"],
        "block_io_ticks": report["import"]["block_io_ticks"],
        "first_five_rows_per_s_median": first_median,
        "last_five_rows_per_s_median": last_median,
        "interval_decline_percent": decline,
        "degradation_confirmed": len(intervals) >= 10 and decline > 15.0,
        "index_add_first_five_median_s": statistics.median(index_first)
        if index_first
        else 0.0,
        "index_add_last_five_median_s": statistics.median(index_last)
        if index_last
        else 0.0,
        "major_timings": {
            "raw_copy_hash": _timing(report, "copy_and_hash"),
            "analysis": _timing(report, "_read_csv_plan"),
            "transform": _timing(report, "_build_artifacts"),
            "arrow_conversion": _timing(report, "arrow_from_pydict"),
            "record_parquet_write": _timing(report, "parquet_record_write"),
            "canonical_parquet_write": _timing(report, "parquet_canonical_write"),
            "exact_index_add": sum(
                float(item["wall_s"]) for item in report["index_batches"]
            ),
            "exact_index_finish": _timing(report, "index_finish"),
            "catalog_transactions": _timing(report, "catalog_transaction"),
            "artifact_checksums": _timing(report, "hash_file"),
            "application_fsync": _timing(report, "fsync_file")
            + _timing(report, "fsync_directory"),
        },
        "artifact_sizes": report["artifact_sizes"],
        "intervals": intervals,
        "index_batches": report["index_batches"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--short", type=Path, action="append", required=True)
    parser.add_argument("--long", type=Path, action="append", required=True)
    parser.add_argument("--cprofile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    short = [_load(path) for path in args.short]
    long = [_load(path) for path in args.long]
    profiled = _load(args.cprofile)
    short_times = [float(item["import"]["wall_s"]) for item in short]
    coefficient = (
        statistics.pstdev(short_times) / statistics.mean(short_times) * 100
        if len(short_times) > 1
        else 0.0
    )
    profile_functions = {}
    for name in (
        "_build_artifacts",
        "flush",
        "normalize_with",
        "normalize_date",
        "normalize_exact_text",
        "normalize_phone",
        "normalize_person_name",
    ):
        value = _profile_function(profiled, name)
        if value is not None:
            profile_functions[name] = value
    result = {
        "summary_version": 1,
        "short_run_count": len(short),
        "short_total_wall_s": short_times,
        "short_total_coefficient_of_variation_percent": coefficient,
        "large_repeat_required": coefficient > 15.0,
        "short_runs": [_run_summary(item) for item in short],
        "long_runs": [_run_summary(item) for item in long],
        "cprofile_overhead": {
            "profiled_total_wall_s": profiled["import"]["wall_s"],
            "low_overhead_total_wall_s": short_times,
        },
        "cprofile_functions": profile_functions,
        "hot_object_counts": profiled["hot_object_counts"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
