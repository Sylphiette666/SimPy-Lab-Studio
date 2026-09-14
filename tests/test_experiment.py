from __future__ import annotations

import csv
import json

from simlab.config import ProjectConfig
from simlab.experiment import ExperimentRunner, aggregate, expand_scenarios


def project_config() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "simulation": {
                "until": 20,
                "arrival_interarrival": {"kind": "deterministic", "value": 2},
                "stations": [
                    {
                        "name": "server",
                        "capacity": 1,
                        "service_time": {"kind": "deterministic", "value": 1},
                    }
                ],
            },
            "experiment": {
                "replications": 3,
                "base_seed": 10,
                "parameter_grid": {"stations.0.capacity": [1, 2]},
            },
        }
    )


def test_grid_and_exports(tmp_path) -> None:
    config = project_config()
    assert len(expand_scenarios(config)) == 2
    runner = ExperimentRunner(config)
    result = runner.run()
    assert len(result["replications"]) == 6
    assert {row["scenario"] for row in result["summary"]} == {
        "s001__stations_0_capacity=1",
        "s002__stations_0_capacity=2",
    }

    output = runner.save(result, tmp_path)
    assert (
        json.loads((output / "results.json").read_text(encoding="utf-8"))["schema_version"] == "1.1"
    )
    with (output / "summary.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert rows


def test_summary_reports_missingness_and_no_zero_width_single_sample_ci() -> None:
    records = [
        {
            "scenario": "base",
            "parameters": {},
            "metrics": {"value": 10.0, "missing": None},
        }
    ]
    catalog = [
        {
            "metric": "value",
            "role": "primary",
            "direction": "higher_is_better",
            "unit": "count",
            "definition": "test",
        },
        {
            "metric": "missing",
            "role": "data_quality",
            "direction": "context_only",
            "unit": "count",
            "definition": "test",
        },
    ]
    rows = {row["metric"]: row for row in aggregate(records, 0.95, catalog)}

    assert rows["value"]["n_total"] == 1
    assert rows["value"]["n_missing"] == 0
    assert rows["value"]["std"] is None
    assert rows["value"]["ci_low"] is None
    assert rows["missing"]["n"] == 0
    assert rows["missing"]["n_missing"] == 1
