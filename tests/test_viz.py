from __future__ import annotations

from pathlib import Path

from simlab.viz import load_results, plot_results

KEY_METRICS = [
    "avg_cycle_time",
    "p95_cycle_time",
    "avg_wait_time",
    "service_level",
    "throughput_per_time_unit",
]


def make_result(*, with_traces: bool = False) -> dict:
    scenarios = ["s001__capacity=2", "s002__capacity=3"]
    summary = []
    for scenario in scenarios:
        for metric in KEY_METRICS:
            summary.append(
                {
                    "scenario": scenario,
                    "parameters": {},
                    "metric": metric,
                    "mean": 20.0,
                    "ci_low": 18.0,
                    "ci_high": 22.0,
                }
            )
        for station in ("desk", "expert"):
            summary.append(
                {
                    "scenario": scenario,
                    "parameters": {},
                    "metric": f"station.{station}.utilization",
                    "mean": 0.8,
                    "ci_low": 0.7,
                    "ci_high": 0.9,
                }
            )
    traces = (
        [
            {
                "case_id": "r0:0",
                "activity": "desk",
                "lifecycle": "start",
                "timestamp": 0.0,
            },
            {
                "case_id": "r0:0",
                "activity": "desk",
                "lifecycle": "complete",
                "timestamp": 3.0,
            },
        ]
        if with_traces
        else []
    )
    return {
        "project_name": "viz-test",
        "config": {
            "simulation": {
                "name": "viz-model",
                "until": 480,
                "warmup": 30,
                "first_arrival_at_zero": True,
                "arrival_interarrival": {"kind": "exponential", "mean": 5.0},
                "stations": [
                    {
                        "name": "desk",
                        "capacity": 1,
                        "service_time": {"kind": "exponential", "mean": 3.0},
                    },
                    {
                        "name": "expert",
                        "capacity": 2,
                        "service_time": {
                            "kind": "triangular",
                            "low": 4.0,
                            "mode": 8.0,
                            "high": 15.0,
                        },
                    },
                ],
                "cycle_time_target": 30,
            }
        },
        "summary": summary,
        "trace_events": traces,
    }


def test_plot_results_generates_all_charts(tmp_path: Path) -> None:
    products = plot_results(make_result(with_traces=True), tmp_path)
    names = {product.name for product in products}
    assert names == {
        "kpi_comparison.png",
        "station_utilization.png",
        "model_structure.png",
        "traces_gantt.png",
        "report.html",
    }
    for product in products:
        assert product.exists() and product.stat().st_size > 0


def test_plot_results_skips_gantt_without_traces(tmp_path: Path) -> None:
    products = plot_results(make_result(with_traces=False), tmp_path)
    assert "traces_gantt.png" not in {product.name for product in products}
    assert (tmp_path / "report.html").exists()


def test_report_html_contains_kpi_table(tmp_path: Path) -> None:
    plot_results(make_result(), tmp_path)
    report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "KPI" in report
    assert "s001__capacity=2" in report
    assert "kpi_comparison.png" in report


def test_load_results_merges_traces_jsonl(tmp_path: Path) -> None:
    result_path = tmp_path / "results.json"
    import json

    result_path.write_text(
        json.dumps(make_result(with_traces=False), ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "traces.jsonl").write_text(
        '{"case_id": "r0:0", "activity": "arrival", "lifecycle": "complete", "timestamp": 0.0}\n',
        encoding="utf-8",
    )
    loaded = load_results(result_path)
    assert len(loaded["trace_events"]) == 1
