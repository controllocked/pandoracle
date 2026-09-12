from __future__ import annotations

import os
import platform
import resource
import sqlite3
import sys
import time
from importlib.metadata import version
from math import ceil
from typing import Any

from pandoracle import __version__
from pandoracle.errors import SearchFailure
from pandoracle.models import SearchClue, SearchOperator, SearchRequest, SemanticType, type_id
from pandoracle.normalize import classify_query
from pandoracle.search import search
from pandoracle.workspace import Workspace


def _percentile(sorted_values: list[float], percentile: float) -> float:
    index = max(0, min(len(sorted_values) - 1, ceil(len(sorted_values) * percentile) - 1))
    return sorted_values[index]


def benchmark_exact(
    workspace: Workspace,
    value: str,
    *,
    semantic_type: str | SemanticType | None,
    dataset: str | None,
    warmup: int = 5,
    iterations: int = 100,
) -> dict[str, Any]:
    if not 0 <= warmup <= 1_000:
        raise ValueError("warmup must be between 0 and 1000")
    if not 1 <= iterations <= 100_000:
        raise ValueError("iterations must be between 1 and 100000")
    if semantic_type is None:
        inferred = classify_query(value)
        if not inferred:
            raise SearchFailure("could not infer a searchable semantic type")
        identifier = inferred[0][0]
    else:
        identifier = semantic_type
    request = SearchRequest(
        (SearchClue(identifier, value, SearchOperator.EXACT),),
        dataset=dataset,
        limit=50,
    )
    for _ in range(warmup):
        search(workspace, request)

    latencies_ms: list[float] = []
    result_count = 0
    for _ in range(iterations):
        started = time.perf_counter_ns()
        result = search(workspace, request)
        elapsed = time.perf_counter_ns() - started
        result_count = len(result.records)
        latencies_ms.append(elapsed / 1_000_000)
    latencies_ms.sort()

    try:
        pyarrow_version = version("pyarrow")
    except Exception:  # pragma: no cover - pyarrow is a required dependency
        pyarrow_version = "unknown"
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "benchmark": "exact-search/v1",
        "cache_state": "warm-process; OS cache not controlled",
        "workspace_id": workspace.manifest.workspace_id,
        "semantic_type": type_id(semantic_type) if semantic_type else "inferred",
        "dataset_scope": dataset or "all-active",
        "query_value_recorded": False,
        "warmup": warmup,
        "iterations": iterations,
        "result_count": result_count,
        "latency_ms": {
            "min": latencies_ms[0],
            "p50": _percentile(latencies_ms, 0.50),
            "p95": _percentile(latencies_ms, 0.95),
            "p99": _percentile(latencies_ms, 0.99),
            "max": latencies_ms[-1],
        },
        "peak_rss_kib": usage.ru_maxrss,
        "environment": {
            "pandoracle": __version__,
            "python": sys.version.split()[0],
            "pyarrow": pyarrow_version,
            "sqlite": sqlite3.sqlite_version,
            "kernel": platform.release(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
        },
    }
