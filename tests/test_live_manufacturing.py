from __future__ import annotations

import json
import math
import random

import pytest

from simlab.live_manufacturing import generate_preview, run_config_study
from simlab.manufacturing import (
    BufferSpec,
    MachineSpec,
    ManufacturingConfig,
    ManufacturingSimulation,
    WeeklyBreak,
    _MachineRuntime,
    case_a_config,
    run_manufacturing_replication,
)


def simple_line(*, warmup: float = 0, replications: int = 1) -> ManufacturingConfig:
    return ManufacturingConfig(
        name="custom-line",
        until_seconds=20,
        warmup_seconds=warmup,
        replications=replications,
        machines=[
            MachineSpec(
                name="lathe", cycle_time_seconds=2, idle_power_kw=2, processing_power_kw=10
            ),
            MachineSpec(name="mill", cycle_time_seconds=3, idle_power_kw=2, processing_power_kw=10),
        ],
        buffers=[BufferSpec(name="transfer", capacity=1, delay_seconds=1)],
    )


def test_sampled_session_preserves_batch_result_and_material_at_every_snapshot() -> None:
    config = case_a_config(until_seconds=90_000, warmup_seconds=1000, replications=1)
    simulation = ManufacturingSimulation(config, seed=123)
    for now in range(0, 90_001, 53):
        frame = simulation.advance(now)
        in_machines = sum(machine["holding_part"] for machine in frame["machines"])
        in_buffers = sum(buffer["level"] for buffer in frame["buffers"])
        assert frame["wip"] == in_machines + in_buffers
        assert frame["admitted_total"] == frame["completed_total"] + frame["wip"]
        assert frame["material_balance_error"] == 0
        assert all(0 <= machine["progress"] <= 1 for machine in frame["machines"])
        assert all(
            0 <= buffer["ready"] <= buffer["level"] <= buffer["capacity"]
            for buffer in frame["buffers"]
        )
    assert simulation.run() == run_manufacturing_replication(config, seed=123)


def test_partial_window_metrics_do_not_include_future_or_warmup() -> None:
    config = simple_line(warmup=5)
    simulation = ManufacturingSimulation(config, seed=1)
    assert simulation.snapshot()["metrics"]["energy_kwh"] == 0
    early = simulation.advance(4)
    assert early["wip"] > 0
    assert early["observation_seconds"] == 0
    assert early["metrics"] == {
        "throughput_per_hour": 0.0,
        "average_wip": 0.0,
        "avg_wip": 0.0,
        "specific_energy_kwh_per_part": None,
        "energy_kwh": 0.0,
    }
    simulation.advance(10)
    partial = simulation.result()
    shorter = ManufacturingConfig.model_validate({**config.model_dump(), "until_seconds": 10})
    assert partial == run_manufacturing_replication(shorter, seed=1)
    assert partial["metrics"]["energy_kwh"] < simulation.run()["metrics"]["energy_kwh"]


def test_progress_freezes_during_break_and_resumes_residual_work() -> None:
    config = ManufacturingConfig(
        until_seconds=20,
        warmup_seconds=0,
        replications=1,
        machines=[MachineSpec(name="M", cycle_time_seconds=10)],
        breaks=[WeeklyBreak(start_second=3, end_second=8)],
    )
    simulation = ManufacturingSimulation(config, seed=1)
    assert simulation.advance(2)["machines"][0]["progress"] == pytest.approx(0.2)
    paused = simulation.advance(6)["machines"][0]
    assert paused["state"] == "off_shift"
    assert paused["progress"] == pytest.approx(0.3)
    assert simulation.advance(10)["machines"][0]["progress"] == pytest.approx(0.5)
    assert simulation.advance(15)["machines"][0]["progress"] == 1
    assert simulation.advance(16)["completed_total"] == 1


def test_progress_freezes_during_failure_and_records_real_failure(monkeypatch) -> None:
    config = ManufacturingConfig(
        until_seconds=20,
        warmup_seconds=0,
        replications=1,
        machines=[MachineSpec(name="M", cycle_time_seconds=10, availability=0.9, mttr_seconds=2)],
    )
    uptimes = iter([3.0, 100.0])
    monkeypatch.setattr(_MachineRuntime, "_next_uptime", lambda self: next(uptimes))
    monkeypatch.setattr(random.Random, "expovariate", lambda self, rate: 2.0)
    simulation = ManufacturingSimulation(config, seed=1)
    failed = simulation.advance(4)["machines"][0]
    assert failed["state"] == "failed"
    assert failed["failures"] == 1
    assert failed["progress"] == pytest.approx(0.3)
    assert simulation.advance(8)["machines"][0]["progress"] == pytest.approx(0.6)


def test_preview_is_deterministic_bounded_and_matches_first_study_replication() -> None:
    config = simple_line(replications=2)
    preview = generate_preview(config, max_frames=17)
    assert preview == generate_preview(config, max_frames=17)
    assert len(preview["frames"]) == 18
    assert preview["frames"][0]["time_seconds"] == 0
    assert preview["frames"][-1]["time_seconds"] == config.until_seconds
    assert not preview["truncated"]
    assert preview["duration_seconds"] == config.until_seconds
    study = run_config_study(config)
    record = study["replications"][0]
    assert preview["seed"] == record["seed"]
    last = preview["frames"][-1]
    assert last["completed_total"] == record["diagnostics"]["completed_total"]
    for metric in ("throughput_per_hour", "avg_wip", "specific_energy_kwh_per_part", "energy_kwh"):
        assert last["metrics"][metric] == record["metrics"][metric]
    json.dumps(preview, allow_nan=False)
    json.dumps(study, allow_nan=False)


def test_short_preview_uses_actual_endpoint_and_does_not_change_config() -> None:
    config = simple_line(warmup=5)
    before = config.model_dump()
    preview = generate_preview(config, seed=17, max_frames=3, preview_seconds=7)
    assert preview["truncated"]
    assert preview["duration_seconds"] == 7
    assert preview["frames"][-1]["time_seconds"] == 7
    assert preview["frames"][-1]["observation_seconds"] == 2
    assert preview["frames"][-1] == ManufacturingSimulation(config, 17).advance(7)
    assert config.model_dump() == before


def test_custom_study_runs_exact_config_with_reproducible_paired_seeds() -> None:
    config = simple_line(replications=3)
    study = run_config_study(config)
    assert len(study["replications"]) == 3
    assert {record["scenario"] for record in study["replications"]} == {"current"}
    assert {record["replication"] for record in study["replications"]} == {0, 1, 2}
    assert all(row["n_total"] == 3 for row in study["summary"])
    changed = config.model_copy(deep=True)
    changed.machines[0].cycle_time_seconds = 8
    slower = run_config_study(changed)
    assert [r["seed"] for r in study["replications"]] == [r["seed"] for r in slower["replications"]]
    assert (
        study["replications"][0]["metrics"]["completed"]
        > slower["replications"][0]["metrics"]["completed"]
    )
    assert study["replications"] == run_config_study(config)["replications"]


@pytest.mark.parametrize("bad_time", [-1, 21, math.inf, math.nan])
def test_invalid_advance_is_rejected(bad_time) -> None:
    with pytest.raises(ValueError):
        ManufacturingSimulation(simple_line(), seed=1).advance(bad_time)


def test_rewind_requires_a_new_session_and_snapshot_does_not_advance() -> None:
    simulation = ManufacturingSimulation(simple_line(), seed=1)
    at_ten = simulation.advance(10)
    assert at_ten == simulation.advance(10) == simulation.snapshot()
    with pytest.raises(ValueError):
        simulation.advance(9)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_frames": 0},
        {"max_frames": True},
        {"max_frames": 1.5},
        {"preview_seconds": 0},
        {"preview_seconds": math.inf},
        {"preview_seconds": True},
        {"seed": -1},
        {"seed": True},
    ],
)
def test_invalid_preview_inputs_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        generate_preview(simple_line(), **kwargs)
