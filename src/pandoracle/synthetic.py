from __future__ import annotations

import csv
import random
from pathlib import Path

FIRST_NAMES = ("Alexey", "Oleksandr", "Maria", "Daria", "Иван", "Олександр")
LAST_NAMES = ("Ivanov", "Kovalenko", "Smith", "Müller", "Петров", "Коваленко")
CITIES = ("Kyiv", "Almaty", "Berlin", "Warsaw", "London")


def generate_csv(path: Path, rows: int, *, seed: int = 20260901) -> int:
    if rows < 0:
        raise ValueError("rows cannot be negative")
    randomizer = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("full_name", "email", "phone", "city", "ip_address"))
        for ordinal in range(rows):
            first = randomizer.choice(FIRST_NAMES)
            last = randomizer.choice(LAST_NAMES)
            duplicate_bucket = ordinal // 7 if ordinal % 13 == 0 else ordinal
            writer.writerow(
                (
                    f"{first} {last}",
                    f"person{duplicate_bucket}@example.test",
                    f"+38050{duplicate_bucket % 10_000_000:07d}",
                    randomizer.choice(CITIES),
                    f"10.{(ordinal // 65536) % 256}.{(ordinal // 256) % 256}.{ordinal % 256}",
                )
            )
    return path.stat().st_size
