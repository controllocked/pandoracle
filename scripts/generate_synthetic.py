#!/usr/bin/env python3
"""Generate deterministic synthetic CSV data for development benchmarks."""

from __future__ import annotations

import argparse
from pathlib import Path

from pandoracle.synthetic import generate_csv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=20260901)
    args = parser.parse_args()
    size = generate_csv(args.output, args.rows, seed=args.seed)
    print(f"Generated {args.rows:,} rows ({size:,} bytes): {args.output}")


if __name__ == "__main__":
    main()
