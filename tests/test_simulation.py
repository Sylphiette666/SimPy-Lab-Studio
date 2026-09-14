from __future__ import annotations

from simlab.config import SimulationConfig
from simlab.simulation import run_replication


def tiny_simulation() -> SimulationConfig:
    return SimulationConfig.model_validate(
        {
            "until": 20,
            "warmup": 0,
            "max_arrivals": 5,
            "arrival_interarrival": {"kind": "deterministic", "value": 2},
            "stations": [
                {
                    "name": "server",
                    "capacity": 1,
                    "service_time": {"kind": "deterministic", "value": 1},
                }
            ],
            "cycle_time_target": 2,
        }
    )


def test_replication_is_deterministic_for_same_seed() -> None:
    simulation = tiny_simulation()
    first = run_replication(simulation, seed=42)
    second = run_replication(simulation, seed=42)
    assert first["metrics"] == second["metrics"]


def test_known_deterministic_kpis() -> None:
    result = run_replication(tiny_simulation(), seed=1)["metrics"]
    assert result["arrivals"] == 5
    assert result["completed"] == 5
    assert result["wip_end"] == 0
    assert result["avg_cycle_time"] == 1
    assert result["avg_total_wait_time"] == 0
    assert result["service_level"] == 1
    assert result["cycle_completion_fraction"] == 1
    assert result["censored_cycle_count"] == 0
    assert result["station"]["server"]["utilization"] == 0.25


def test_unfinished_cohort_is_reported_as_censored() -> None:
    simulation = SimulationConfig.model_validate(
        {
            "until": 3,
            "arrival_interarrival": {"kind": "deterministic", "value": 1},
            "stations": [
                {
                    "name": "server",
                    "service_time": {"kind": "deterministic", "value": 10},
                }
            ],
        }
    )
    result = run_replication(simulation, seed=1)["metrics"]
    assert result["observed_cycle_count"] == 0
    assert result["censored_cycle_count"] == 3
    assert result["cycle_completion_fraction"] == 0
