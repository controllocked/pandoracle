from pathlib import Path

from pandoracle.synthetic import generate_csv


def test_synthetic_generator_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    generate_csv(first, 100, seed=42)
    generate_csv(second, 100, seed=42)
    assert first.read_bytes() == second.read_bytes()
