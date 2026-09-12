#!/usr/bin/env python3
"""Run the development exact-search microbenchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pandoracle.benchmark import benchmark_exact
from pandoracle.workspace import Workspace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("value", help="Exact value; it is omitted from the report")
    parser.add_argument("--type", dest="semantic_type")
    parser.add_argument("--dataset")
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    result = benchmark_exact(
        Workspace.open(args.workspace),
        args.value,
        semantic_type=args.semantic_type,
        dataset=args.dataset,
        warmup=args.warmup,
        iterations=args.iterations,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
