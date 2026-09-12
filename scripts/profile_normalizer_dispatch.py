#!/usr/bin/env python3
"""Compare current canonicalizer dispatch with a precomputed lookup table."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandoracle.normalize as normalizers

CASES = (
    ("person-name/v1", "Synthetic Person 000042"),
    ("date/v1", "1990-04-19"),
    ("exact-text/v1", "000000000042.0"),
    ("phone/v1", "+70000000042"),
    ("exact-text/v1", "Synthetic-A"),
    ("exact-text/v1", "Synthetic group 07"),
    (
        "exact-text/v1",
        "SYNTHETIC COUNTRY: TESTLAND, CITY: GENERATED-0042, "
        "STREET: DETERMINISTIC BENCHMARK AVENUE 00042",
    ),
)

CACHED = {
    "email/v1": normalizers.normalize_email,
    "phone/v1": normalizers.normalize_phone,
    "domain/v1": normalizers.normalize_domain,
    "url/v1": normalizers.normalize_url,
    "username/v1": normalizers.normalize_username,
    "ip/v1": normalizers.normalize_ip,
    "date/v1": normalizers.normalize_date,
    "person-name/v1": normalizers.normalize_person_name,
    "exact-text/v1": normalizers.normalize_exact_text,
}


def _current(rows: int) -> None:
    for _ in range(rows):
        for canonicalizer_id, value in CASES:
            normalizers.normalize_with(canonicalizer_id, 1, value)


def _cached(rows: int) -> None:
    for _ in range(rows):
        for canonicalizer_id, value in CASES:
            CACHED[canonicalizer_id](value)


def _measure(function: Callable[[int], None], rows: int) -> dict[str, float]:
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    function(rows)
    return {
        "wall_s": time.perf_counter() - wall_started,
        "process_cpu_s": time.process_time() - cpu_started,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=20_000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    if args.rows <= 0 or args.rounds <= 0:
        parser.error("--rows and --rounds must be positive")

    _current(1_000)
    _cached(1_000)
    rounds = []
    for index in range(args.rounds):
        order = (("current", _current), ("cached", _cached))
        if index % 2:
            order = tuple(reversed(order))
        result: dict[str, Any] = {"round": index + 1, "order": [item[0] for item in order]}
        for name, function in order:
            result[name] = _measure(function, args.rows)
        result["wall_speedup"] = result["current"]["wall_s"] / result["cached"]["wall_s"]
        result["cpu_speedup"] = (
            result["current"]["process_cpu_s"] / result["cached"]["process_cpu_s"]
        )
        rounds.append(result)

    report = {
        "rows_per_round": args.rows,
        "useful_values_per_round": args.rows * len(CASES),
        "rounds": rounds,
        "median_wall_speedup": statistics.median(item["wall_speedup"] for item in rounds),
        "median_cpu_speedup": statistics.median(item["cpu_speedup"] for item in rounds),
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.result)


if __name__ == "__main__":
    main()
