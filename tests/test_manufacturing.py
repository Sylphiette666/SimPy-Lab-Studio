from __future__ import annotations

import json
import math
import random

import pytest
from pydantic import ValidationError

from simlab.manufacturing import (
    WEEK,
    BufferSpec,
    MachineSpec,
    ManufacturingConfig,
    WeeklyBreak,
    _MachineRuntime,
    case_a_config,
    case_a_scenarios,
    run_manufacturing_replication,
    run_manufacturing_study,
    save_manufacturing_study,
    validate_case_a_adaptation,
)


def line(
    cycles: tuple[float, ...] = (2,),
    *,
    until: float = 10,
    warmup: float = 0,
    capacity: int = 1,
    delay: float = 0,
    breaks: list[WeeklyBreak] | None = None,
) -> ManufacturingConfig:
    return ManufacturingConfig(
        until_seconds=until,
        warmup_seconds=warmup,
        replications=1,
        machines=[
            MachineSpec(
                name=f"M{index}", cycle_time_seconds=cycle, idle_power_kw=2, processing_power_kw=10
            )
            for index, cycle in enumerate(cycles)
        ],
        buffers=[
            BufferSpec(name=f"B{index}", capacity=capacity, delay_seconds=delay)
            for index in range(len(cycles) - 1)
        ],
        breaks=breaks or [],
    )


def test_single_machine_known_kpis_and_open_end_horizon() -> None:
    result = run_manufacturing_replication(line(), seed=1)
    metrics = result["metrics"]
    # Completions at 2, 4, 6, 8; an event at the right endpoint 10 is excluded.
    assert metrics["completed"] == 4
    assert metrics["throughput_per_hour"] == 1440
    assert metrics["avg_wip"] == 1
    assert metrics["wip_end"] == 1
    assert metrics["energy_kwh"] == pytest.approx(100 / 3600)
    assert metrics["specific_energy_kwh_per_part"] == pytest.approx(25 / 3600)
    assert metrics["machine"]["M0"]["utilization"] == 1
    assert result["diagnostics"]["material_balance_error"] == 0


def test_serial_line_hand_calculated_wip_and_energy() -> None:
    result = run_manufacturing_replication(line((2, 3)), seed=9)
    metrics = result["metrics"]
    assert metrics["completed"] == 2  # Exits at t = 5 and t = 8.
    assert metrics["avg_wip"] == pytest.approx(2.3)
    assert metrics["energy_kwh"] == pytest.approx((100 + 84) / 3600)
    assert metrics["machine"]["M1"]["state_seconds"]["starved"] == 2
    assert metrics["buffer"]["B0"]["max_occupancy"] == 1


def test_finite_buffer_blocks_upstream_and_conserves_material() -> None:
    result = run_manufacturing_replication(line((1, 10), until=100), seed=4)
    metrics = result["metrics"]
    assert metrics["machine"]["M0"]["state_seconds"]["blocked"] > 70
    assert metrics["buffer"]["B0"]["max_occupancy"] <= 1
    assert metrics["wip_end"] <= 3
    assert metrics["avg_wip"] <= 3
    diagnostics = result["diagnostics"]
    assert diagnostics["admitted_total"] == diagnostics["completed_total"] + metrics["wip_end"]
    assert metrics["wip_end"] == diagnostics["in_machines_end"] + diagnostics["in_buffers_end"]


def test_transfer_delays_overlap_and_count_toward_capacity() -> None:
    result = run_manufacturing_replication(
        line((1, 1), until=20, capacity=10, delay=10),
        seed=2,
    )
    # Concurrent transport delivers the first item at 11 and finishes it at 12;
    # serializing transport would produce only one finished item by t = 20.
    assert result["metrics"]["completed"] == 8
    buffer = result["metrics"]["buffer"]["B0"]
    assert buffer["max_occupancy"] == 10
    assert buffer["avg_occupancy"] > 7
    assert result["diagnostics"]["material_balance_error"] == 0


def test_break_interrupts_and_resumes_remaining_processing() -> None:
    config = line((10,), until=20, breaks=[WeeklyBreak(start_second=3, end_second=8)])
    metrics = run_manufacturing_replication(config, seed=1)["metrics"]
    assert metrics["completed"] == 1  # 3 s before the break + 7 s after it => finish at 15.
    state = metrics["machine"]["M0"]["state_seconds"]
    assert state["processing"] == 15
    assert state["off_shift"] == 5
    assert metrics["energy_kwh"] == pytest.approx((15 * 10 + 5 * 2) / 3600)
    assert sum(metrics["machine"]["M0"]["state_fractions"].values()) == pytest.approx(1)


def test_calendar_repeats_without_negative_delays_over_multiple_weeks() -> None:
    config = line((WEEK,), until=2 * WEEK + 9, breaks=[WeeklyBreak(start_second=3, end_second=8)])
    metrics = run_manufacturing_replication(config, seed=1)["metrics"]
    assert metrics["completed"] == 1
    assert metrics["machine"]["M0"]["state_seconds"]["off_shift"] == 15
    assert metrics["machine"]["M0"]["state_seconds"]["processing"] == 2 * WEEK - 6


def test_warmup_clips_existing_parts_states_and_energy_without_resetting_line() -> None:
    config = line((10,), until=20, warmup=5, breaks=[WeeklyBreak(start_second=3, end_second=8)])
    metrics = run_manufacturing_replication(config, seed=1)["metrics"]
    assert metrics["completed"] == 1
    assert metrics["avg_wip"] == 1
    assert metrics["machine"]["M0"]["state_seconds"]["off_shift"] == 3
    assert metrics["machine"]["M0"]["state_seconds"]["processing"] == 12
    assert metrics["energy_kwh"] == pytest.approx((12 * 10 + 3 * 2) / 3600)


def test_forced_failure_resumes_instead_of_restarting_part(monkeypatch) -> None:
    config = line((10,), until=13)
    config.machines[0].availability = 0.9
    config.machines[0].mttr_seconds = 2
    uptimes = iter([3.0, 100.0])
    monkeypatch.setattr(_MachineRuntime, "_next_uptime", lambda self: next(uptimes))
    monkeypatch.setattr(random.Random, "expovariate", lambda self, rate: 2.0)
    metrics = run_manufacturing_replication(config, seed=1)["metrics"]
    assert metrics["completed"] == 1  # completion at 12, not 15.
    machine = metrics["machine"]["M0"]
    assert machine["state_seconds"]["failed"] == 2
    assert machine["state_seconds"]["processing"] == 11
    assert machine["failures_total"] == machine["repairs_total"] == 1
    assert metrics["energy_kwh"] == pytest.approx((11 * 10 + 2 * 2) / 3600)


def test_calendar_time_repair_continues_through_break(monkeypatch) -> None:
    config = line((10,), until=21, breaks=[WeeklyBreak(start_second=5, end_second=10)])
    config.machines[0].availability = 0.9
    config.machines[0].mttr_seconds = 10
    uptimes = iter([3.0, 100.0])
    monkeypatch.setattr(_MachineRuntime, "_next_uptime", lambda self: next(uptimes))
    monkeypatch.setattr(random.Random, "expovariate", lambda self, rate: 10.0)
    metrics = run_manufacturing_replication(config, seed=1)["metrics"]
    assert metrics["completed"] == 1  # failure 3, repaired 13, residual processing ends 20.
    state = metrics["machine"]["M0"]["state_seconds"]
    assert state["off_shift"] == 5
    assert state["failed"] == 5
    assert state["processing"] == 11


def test_no_buffer_retrieval_during_break() -> None:
    config = line(
        (1, 1), until=9, capacity=1, delay=5, breaks=[WeeklyBreak(start_second=3, end_second=8)]
    )
    result = run_manufacturing_replication(config, seed=1)
    # The part becomes ready at 6 but is retrieved only when production resumes at 8.
    assert result["metrics"]["completed"] == 0
    assert result["metrics"]["buffer"]["B0"]["avg_occupancy"] == pytest.approx(8 / 9)
    assert result["metrics"]["machine"]["M1"]["state_seconds"]["off_shift"] == 5
    assert result["diagnostics"]["material_balance_error"] == 0


def test_zero_completions_has_missing_sec_not_zero_or_infinity() -> None:
    metrics = run_manufacturing_replication(line((100,), until=10), seed=4)["metrics"]
    assert metrics["completed"] == 0
    assert metrics["specific_energy_kwh_per_part"] is None
    assert math.isfinite(metrics["energy_kwh"])


def test_seeded_failures_reproduce_and_streams_are_independent() -> None:
    config = case_a_config(until_seconds=30_000, warmup_seconds=1000, replications=1)
    first = run_manufacturing_replication(config, seed=11)
    assert first == run_manufacturing_replication(config, seed=11)
    assert first["metrics"] != run_manufacturing_replication(config, seed=12)["metrics"]
    assert sum(m["failures_total"] for m in first["metrics"]["machine"].values()) > 0
    for machine in first["metrics"]["machine"].values():
        assert sum(machine["state_fractions"].values()) == pytest.approx(1)


def test_case_a_parameters_and_constraint_gate() -> None:
    config = case_a_config()
    assert config.until_seconds == 30 * 86400
    assert config.warmup_seconds == 86400
    assert config.replications == 25
    assert [machine.cycle_time_seconds for machine in config.machines] == [320, 290, 134, 41]
    assert [buffer.capacity for buffer in config.buffers] == [5, 5, 5]
    assert config.machines[0].mttf_seconds == pytest.approx(1409 * 0.85 / 0.15)
    assert [item.name for item in case_a_scenarios()] == ["baseline", "V2", "V3"]
    variants = case_a_scenarios(include_infeasible=True)
    assert [item.name for item in variants] == ["baseline", "V1", "V2", "V3"]
    assert not variants[1].feasible
    assert len(variants[1].violations) == 3
    assert validate_case_a_adaptation(variants[2].config) == []
    assert validate_case_a_adaptation(variants[3].config) == []
    with pytest.raises(ValueError, match="Invalid Case A baseline"):
        case_a_scenarios(variants[1].config)
    changed = config.model_copy(deep=True)
    changed.machines.reverse()
    assert "topology" in " ".join(validate_case_a_adaptation(changed))


def test_study_aggregation_pairing_infeasible_exclusion_and_exports(tmp_path) -> None:
    config = case_a_config(until_seconds=20_000, warmup_seconds=1000, replications=2)
    result = run_manufacturing_study(config, include_infeasible=True)
    assert len(result["replications"]) == 8
    assert "V1" not in result["evaluation"]["eligible_scenarios"]
    assert result["evaluation"]["highest_mean_throughput_feasible_scenario"] != "V1"
    seeds = {(item["replication"], item["seed"]) for item in result["replications"]}
    assert len(seeds) == 2
    for row in result["summary"]:
        assert row["n_total"] == 2
    save_manufacturing_study(result, tmp_path)
    loaded = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert loaded["config"] == config.model_dump()
    assert loaded["scenarios"][1]["feasible"] is False
    assert "不可推荐" in (tmp_path / "comparison.md").read_text(encoding="utf-8")
    assert (tmp_path / "replications.csv").exists()
    assert (tmp_path / "summary.csv").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"warmup_seconds": 10, "until_seconds": 10},
        {"replications": True},
        {"until_seconds": math.inf},
        {"buffers": []},
        {"breaks": [{"start_second": 2, "end_second": 5}, {"start_second": 4, "end_second": 8}]},
    ],
)
def test_invalid_manufacturing_inputs_rejected(changes) -> None:
    with pytest.raises(ValidationError):
        case_a_config(**changes)


def test_serial_and_parallel_study_match() -> None:
    config = case_a_config(until_seconds=5000, warmup_seconds=100, replications=2)
    serial = run_manufacturing_study(config)
    parallel = run_manufacturing_study(config, workers=2)
    assert serial["replications"] == parallel["replications"]
    assert serial["summary"] == parallel["summary"]
