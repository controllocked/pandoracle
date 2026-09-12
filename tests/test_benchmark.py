import csv
from pathlib import Path

from pandoracle.benchmark import benchmark_exact
from pandoracle.ingest import analyze_csv, import_csv
from pandoracle.models import SemanticType
from pandoracle.schema import confirm_plan
from pandoracle.workspace import Workspace


def test_benchmark_omits_query_value(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("email",))
        writer.writerow(("secret-query@example.test",))
    workspace = Workspace.create(tmp_path / "workspace")
    import_csv(workspace, source, schema_plan=confirm_plan(analyze_csv(source)))
    report = benchmark_exact(
        workspace,
        "secret-query@example.test",
        semantic_type=SemanticType.EMAIL,
        dataset=None,
        warmup=1,
        iterations=3,
    )
    assert report["result_count"] == 1
    assert report["query_value_recorded"] is False
    assert "secret-query" not in str(report)
    assert report["latency_ms"]["p95"] >= 0
