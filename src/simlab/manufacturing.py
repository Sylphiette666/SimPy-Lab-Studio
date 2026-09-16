"""Independent event-driven implementation of the paper's Case A production line.

All times are seconds and power is kW. This module deliberately uses its own
strict schema so that the existing service-system simulator keeps its semantics.
The paper does not specify every operational detail; ASSUMPTIONS records the
choices required to make this implementation reproducible.
"""

from __future__ import annotations

import csv
import json
import math
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import simpy
from pydantic import BaseModel, ConfigDict, Field, model_validator

from simlab.experiment import aggregate, flatten_numeric
from simlab.rng import derive_seed

DAY = 86_400.0
WEEK = 7 * DAY
ASSUMPTIONS = [
    "All model times are seconds; simulation time zero is Monday 00:00.",
    "The horizon includes warm-up; all KPI integrals use [warmup_seconds, until_seconds).",
    "Throughput uses elapsed calendar hours, including scheduled production breaks.",
    "Raw stock is replenished on demand and never starves machine 1; the bounded raw stock "
    "and completed sink are excluded from production WIP.",
    "A buffer slot is reserved at admission and remains occupied during its transfer delay; "
    "different parts transfer concurrently. Machines block after processing until a slot is free.",
    "Exponential uptime is measured in accumulated processing time; MTTF = "
    "availability * MTTR / (1 - availability). Repairs are independent exponential calendar times.",
    "Interrupted processing resumes its remaining duration after repair or a scheduled break; "
    "repairs continue during breaks, while production and buffer admission/retrieval pause.",
    "Processing uses processing_power_kw; all other states, including failures and breaks, "
    "use idle_power_kw. Energy and WIP are integrated exactly between events.",
    "The Case A process constraints keep the original serial topology and intermediate buffer "
    "capacities. V1 is an explicitly infeasible diagnostic scenario, "
    "never a feasible recommendation.",
    "The published KPIs are reference observations, not calibrated targets or proof of "
    "numerical reproduction; the paper does not disclose all timing semantics or random seeds.",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MachineSpec(_StrictModel):
    name: str = Field(min_length=1)
    cycle_time_seconds: float = Field(gt=0)
    availability: float = Field(default=1.0, gt=0, le=1)
    mttr_seconds: float = Field(default=0.0, ge=0)
    idle_power_kw: float = Field(default=0.0, ge=0)
    processing_power_kw: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def repair_required(self) -> MachineSpec:
        if self.availability < 1 and self.mttr_seconds <= 0:
            raise ValueError("availability < 1 requires mttr_seconds > 0")
        return self

    @property
    def mttf_seconds(self) -> float:
        if self.availability == 1:
            return math.inf
        return self.availability * self.mttr_seconds / (1 - self.availability)


class BufferSpec(_StrictModel):
    name: str = Field(min_length=1)
    capacity: int = Field(ge=1, strict=True)
    delay_seconds: float = Field(default=0.0, ge=0)


class WeeklyBreak(_StrictModel):
    """A non-wrapping interval in a Monday-anchored week; split wrapping intervals."""

    start_second: float = Field(ge=0, lt=WEEK)
    end_second: float = Field(gt=0, le=WEEK)

    @model_validator(mode="after")
    def ordered(self) -> WeeklyBreak:
        if self.end_second <= self.start_second:
            raise ValueError("weekly break end must follow its start")
        return self


class ManufacturingConfig(_StrictModel):
    name: str = "case-a-machining-line"
    until_seconds: float = Field(default=30 * DAY, gt=0)
    warmup_seconds: float = Field(default=DAY, ge=0)
    replications: int = Field(default=25, ge=1, strict=True)
    base_seed: int = Field(default=20260909, ge=0, strict=True)
    confidence_level: float = Field(default=0.95, gt=0, lt=1)
    machines: list[MachineSpec] = Field(min_length=1)
    buffers: list[BufferSpec] = Field(default_factory=list)
    breaks: list[WeeklyBreak] = Field(default_factory=list)
    raw_buffer_capacity: int = Field(default=1000, ge=1, strict=True)

    @model_validator(mode="after")
    def consistent_line(self) -> ManufacturingConfig:
        if self.warmup_seconds >= self.until_seconds:
            raise ValueError("warmup_seconds must be smaller than until_seconds")
        if len(self.buffers) != len(self.machines) - 1:
            raise ValueError("a serial line needs exactly one buffer between consecutive machines")
        for items, label in ((self.machines, "machine"), (self.buffers, "buffer")):
            names = [item.name for item in items]
            if len(names) != len(set(names)):
                raise ValueError(f"{label} names must be unique")
        intervals = sorted(self.breaks, key=lambda item: item.start_second)
        for left, right in zip(intervals, intervals[1:], strict=False):
            if left.end_second > right.start_second:
                raise ValueError("weekly breaks may not overlap")
        if sum(item.end_second - item.start_second for item in intervals) >= WEEK:
            raise ValueError("the weekly calendar must contain some production time")
        return self


class ManufacturingScenario(_StrictModel):
    name: str
    description: str
    feasible: bool
    violations: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    config: ManufacturingConfig


def case_a_config(**overrides: Any) -> ManufacturingConfig:
    """Return the paper's Table 4/Appendix C inputs with explicit timing assumptions."""
    data: dict[str, Any] = {
        "machines": [
            {
                "name": name,
                "cycle_time_seconds": cycle,
                "availability": availability,
                "mttr_seconds": mttr,
                "idle_power_kw": 10,
                "processing_power_kw": 50,
            }
            for name, cycle, availability, mttr in (
                ("SV36262", 320, 0.85, 1409),
                ("SV32173", 290, 0.85, 718),
                ("SV91991", 134, 0.98, 6717),
                ("SV91989", 41, 0.99, 671),
            )
        ],
        "buffers": [
            {"name": f"buffer{index}", "capacity": 5, "delay_seconds": 10} for index in range(1, 4)
        ],
        "breaks": [
            {"start_second": 4 * DAY + 17 * 3600, "end_second": 5 * DAY + 7 * 3600},
            {"start_second": 5 * DAY + 17 * 3600, "end_second": 6 * DAY + 7 * 3600},
        ],
    }
    data.update(overrides)
    return ManufacturingConfig.model_validate(data)


def validate_case_a_adaptation(
    candidate: ManufacturingConfig,
    reference: ManufacturingConfig | None = None,
) -> list[str]:
    """Return local process-constraint violations without running or applying a change."""
    reference = reference or case_a_config()
    violations = []
    if [m.name for m in candidate.machines] != [m.name for m in reference.machines]:
        violations.append("Case A must retain its original four-machine serial topology.")
    if [b.name for b in candidate.buffers] != [b.name for b in reference.buffers]:
        violations.append("Case A must retain its original intermediate buffer sequence.")
    original = {buffer.name: buffer.capacity for buffer in reference.buffers}
    for buffer in candidate.buffers:
        if buffer.name in original and buffer.capacity != original[buffer.name]:
            violations.append(
                f"CPD: {buffer.name} capacity must remain {original[buffer.name]} "
                f"(proposed {buffer.capacity})."
            )
    return violations


def case_a_scenarios(
    config: ManufacturingConfig | None = None,
    *,
    include_infeasible: bool = False,
) -> list[ManufacturingScenario]:
    config = ManufacturingConfig.model_validate((config or case_a_config()).model_dump())
    violations = validate_case_a_adaptation(config)
    if violations:
        raise ValueError("Invalid Case A baseline: " + " ".join(violations))
    scenarios = [
        ManufacturingScenario(
            name="baseline",
            description="Paper Case A baseline",
            feasible=True,
            config=config,
        )
    ]
    specifications = [
        (
            "V1",
            "All intermediate buffers: capacity 5 to 10 (CPD violation)",
            {"buffers.capacity": 10},
        ),
        ("V2", "SV36262 availability: 85% to 90%", {"SV36262.availability": 0.90}),
        ("V3", "SV32173 availability: 85% to 90%", {"SV32173.availability": 0.90}),
    ]
    for name, description, parameters in specifications:
        if name == "V1" and not include_infeasible:
            continue
        data = config.model_dump()
        if name == "V1":
            for buffer in data["buffers"]:
                buffer["capacity"] = 10
        else:
            data["machines"][0 if name == "V2" else 1]["availability"] = 0.90
        candidate = ManufacturingConfig.model_validate(data)
        violations = validate_case_a_adaptation(candidate)
        scenarios.append(
            ManufacturingScenario(
                name=name,
                description=description,
                parameters=parameters,
                feasible=not violations,
                violations=violations,
                config=candidate,
            )
        )
    return scenarios


class _Calendar:
    def __init__(self, breaks: list[WeeklyBreak]):
        self.breaks = sorted(breaks, key=lambda interval: interval.start_second)

    def is_open(self, now: float) -> bool:
        phase = now % WEEK
        return not any(item.start_second <= phase < item.end_second for item in self.breaks)

    def next_change(self, now: float) -> float:
        if not self.breaks:
            return math.inf
        week_start = math.floor(now / WEEK) * WEEK
        phase = now - week_start
        for item in self.breaks:
            if item.start_second <= phase < item.end_second:
                return week_start + item.end_second
            if item.start_second > phase:
                return week_start + item.start_second
        return week_start + WEEK + self.breaks[0].start_second


@dataclass
class _Level:
    warmup: float
    until: float
    value: int = 0
    area: float = 0.0
    last: float = 0.0
    maximum: int = 0

    def change(self, delta: int, now: float) -> None:
        self.area += self.value * max(0.0, min(now, self.until) - max(self.last, self.warmup))
        self.last = now
        self.value += delta
        self.maximum = max(self.maximum, self.value)
        if self.value < 0:
            raise RuntimeError("negative material inventory")

    def integral(self, now: float | None = None) -> float:
        end = self.until if now is None else min(now, self.until)
        return self.area + self.value * max(0.0, end - max(self.last, self.warmup))


class _FiniteBuffer:
    """Capacity includes every part in transit and every part ready for retrieval."""

    def __init__(self, env: simpy.Environment, spec: BufferSpec, config: ManufacturingConfig):
        self.env = env
        self.spec = spec
        self.slots = simpy.Container(env, capacity=spec.capacity, init=spec.capacity)
        self.ready = simpy.Store(env, capacity=spec.capacity)
        self.level = _Level(config.warmup_seconds, config.until_seconds)

    def admit(self, part: int) -> None:
        # A caller has already reserved one slot via slots.get(1).
        self.level.change(1, self.env.now)
        if self.level.value > self.spec.capacity:
            raise RuntimeError("buffer capacity exceeded")
        self.env.process(self._transfer(part))

    def _transfer(self, part: int):
        yield self.env.timeout(self.spec.delay_seconds)
        yield self.ready.put(part)

    def retrieve(self) -> None:
        self.level.change(-1, self.env.now)
        self.slots.put(1)


@dataclass
class _LineCounters:
    config: ManufacturingConfig
    wip: _Level = field(init=False)
    admitted: int = 0
    completed_total: int = 0
    completed_window: int = 0

    def __post_init__(self) -> None:
        self.wip = _Level(self.config.warmup_seconds, self.config.until_seconds)

    def admit(self, now: float) -> int:
        part = self.admitted
        self.admitted += 1
        self.wip.change(1, now)
        return part

    def complete(self, now: float) -> None:
        self.completed_total += 1
        if self.config.warmup_seconds <= now < self.config.until_seconds:
            self.completed_window += 1
        self.wip.change(-1, now)


class _MachineRuntime:
    STATES = ("processing", "starved", "blocked", "failed", "off_shift")

    def __init__(
        self,
        env: simpy.Environment,
        spec: MachineSpec,
        config: ManufacturingConfig,
        calendar: _Calendar,
        counters: _LineCounters,
        input_buffer: _FiniteBuffer | None,
        output_buffer: _FiniteBuffer | None,
        seed: int,
    ):
        self.env = env
        self.spec = spec
        self.config = config
        self.calendar = calendar
        self.counters = counters
        self.input = input_buffer
        self.output = output_buffer
        self.uptime_rng = random.Random(derive_seed(seed, f"manufacturing:uptime:{spec.name}"))
        self.repair_rng = random.Random(derive_seed(seed, f"manufacturing:repair:{spec.name}"))
        self.uptime_left = self._next_uptime()
        self.state = "starved"
        self.state_since = 0.0
        self.durations = dict.fromkeys(self.STATES, 0.0)
        self.holding_part = False
        self.processing_remaining = 0.0
        self.segment_started = 0.0
        self.segment_duration = 0.0
        self.failures = 0
        self.repairs = 0
        env.process(self._run())

    def _next_uptime(self) -> float:
        if self.spec.availability == 1:
            return math.inf
        # Strictly positive draws avoid zero-duration event loops on pathological RNG draws.
        return max(self.uptime_rng.expovariate(1 / self.spec.mttf_seconds), 1e-12)

    def _set_state(self, state: str) -> None:
        start = max(self.state_since, self.config.warmup_seconds)
        end = min(self.env.now, self.config.until_seconds)
        self.durations[self.state] += max(0.0, end - start)
        self.state = state
        self.state_since = self.env.now

    def _wait_open(self):
        while not self.calendar.is_open(self.env.now):
            self._set_state("off_shift")
            yield self.env.timeout(self.calendar.next_change(self.env.now) - self.env.now)

    def _request_during_production(self, factory, state: str):
        """Cancel pending material requests at shift closure, so no overnight transfers occur."""
        while True:
            yield from self._wait_open()
            self._set_state(state)
            request = factory()
            boundary = self.calendar.next_change(self.env.now)
            if math.isinf(boundary):
                result = yield request
                return result
            closure = self.env.timeout(boundary - self.env.now)
            yield request | closure
            # A transfer exactly at a boundary is instantaneous; the next processing
            # segment still waits for the next open shift. Never discard a triggered get.
            if request.triggered:
                return request.value
            request.cancel()

    def _repair(self):
        repair_left = max(self.repair_rng.expovariate(1 / self.spec.mttr_seconds), 1e-12)
        while repair_left > 0:
            self._set_state("failed" if self.calendar.is_open(self.env.now) else "off_shift")
            boundary = self.calendar.next_change(self.env.now)
            duration = min(repair_left, boundary - self.env.now)
            yield self.env.timeout(duration)
            repair_left = max(0.0, repair_left - duration)
        self.repairs += 1
        self.uptime_left = self._next_uptime()

    def _process(self):
        remaining = self.spec.cycle_time_seconds
        self.processing_remaining = remaining
        while remaining > 0:
            yield from self._wait_open()
            if self.uptime_left <= 1e-10:
                self.failures += 1
                yield from self._repair()
                continue
            self._set_state("processing")
            boundary = self.calendar.next_change(self.env.now)
            duration = min(remaining, self.uptime_left, boundary - self.env.now)
            self.segment_started = self.env.now
            self.segment_duration = duration
            yield self.env.timeout(duration)
            remaining = max(0.0, remaining - duration)
            self.processing_remaining = remaining
            self.uptime_left = max(0.0, self.uptime_left - duration)

    def _run(self):
        while True:
            yield from self._wait_open()
            if self.input is None:
                part = self.counters.admit(self.env.now)
            else:
                part = yield from self._request_during_production(self.input.ready.get, "starved")
                self.input.retrieve()
            self.holding_part = True
            yield from self._process()
            if self.output is None:
                self.counters.complete(self.env.now)
            else:
                yield from self._request_during_production(
                    lambda: self.output.slots.get(1),
                    "blocked",
                )
                self.output.admit(part)
            self.holding_part = False

    def progress(self) -> float:
        if not self.holding_part:
            return 0.0
        remaining = self.processing_remaining
        if self.state == "processing":
            remaining -= min(self.env.now - self.segment_started, self.segment_duration)
        return min(1.0, max(0.0, 1 - remaining / self.spec.cycle_time_seconds))

    def metrics(self, now: float | None = None) -> dict[str, Any]:
        end = self.config.until_seconds if now is None else min(now, self.config.until_seconds)
        durations = dict(self.durations)
        durations[self.state] += max(
            0.0,
            end - max(self.state_since, self.config.warmup_seconds),
        )
        window = max(0.0, end - self.config.warmup_seconds)
        processing = durations["processing"]
        energy = (
            processing * self.spec.processing_power_kw
            + (window - processing) * self.spec.idle_power_kw
        ) / 3600
        return {
            "utilization": processing / window if window else 0.0,
            "state_fractions": {
                name: duration / window if window else 0.0 for name, duration in durations.items()
            },
            "state_seconds": durations,
            "energy_kwh": energy,
            "failures_total": self.failures,
            "repairs_total": self.repairs,
            "holding_part_end": int(self.holding_part),
        }


class ManufacturingSimulation:
    """An inspectable session using the same event engine as batch replication.

    ``advance(t)`` evaluates events strictly before ``t``, matching SimPy's numeric
    run-until behavior and the existing half-open observation window. Sampling does
    not consume random numbers or modify production events. Repeated advances keep
    the current parts, repairs, calendar and event queue; changing a configuration
    requires a new session.
    """

    def __init__(self, config: ManufacturingConfig, seed: int):
        self.config = ManufacturingConfig.model_validate(config.model_dump())
        self.seed = seed
        self.env = simpy.Environment()
        self.calendar = _Calendar(self.config.breaks)
        self.counters = _LineCounters(self.config)
        self.buffers = [_FiniteBuffer(self.env, spec, self.config) for spec in self.config.buffers]
        self.machines = [
            _MachineRuntime(
                self.env,
                spec,
                self.config,
                self.calendar,
                self.counters,
                self.buffers[index - 1] if index else None,
                self.buffers[index] if index < len(self.buffers) else None,
                seed,
            )
            for index, spec in enumerate(self.config.machines)
        ]

    def advance(self, time_seconds: float) -> dict[str, Any]:
        """Advance monotonically to an absolute time and return a real snapshot."""
        if not math.isfinite(time_seconds):
            raise ValueError("time_seconds must be finite")
        if not self.env.now <= time_seconds <= self.config.until_seconds:
            raise ValueError("time_seconds must be between current time and configured horizon")
        if time_seconds > self.env.now:
            self.env.run(until=time_seconds)
        return self.snapshot()

    def result(self, replication: int = 0, scenario: str = "base") -> dict[str, Any]:
        """Read KPIs up to now, excluding warm-up and without integrating the future."""
        now = self.env.now
        counters = self.counters
        window = max(0.0, now - self.config.warmup_seconds)
        machine_metrics = {machine.spec.name: machine.metrics(now) for machine in self.machines}
        energy = sum(item["energy_kwh"] for item in machine_metrics.values())
        buffer_metrics = {
            buffer.spec.name: {
                "capacity": buffer.spec.capacity,
                "wip_end": buffer.level.value,
                "avg_occupancy": buffer.level.integral(now) / window if window else 0.0,
                "max_occupancy": buffer.level.maximum,
            }
            for buffer in self.buffers
        }
        in_machines = sum(machine.holding_part for machine in self.machines)
        in_buffers = sum(buffer.level.value for buffer in self.buffers)
        if counters.wip.value != in_machines + in_buffers:
            raise RuntimeError("material conservation failed between machines and buffers")
        return {
            "scenario": scenario,
            "parameters": {},
            "replication": replication,
            "seed": self.seed,
            "metrics": {
                "completed": counters.completed_window,
                "throughput_per_hour": counters.completed_window * 3600 / window if window else 0.0,
                "avg_wip": counters.wip.integral(now) / window if window else 0.0,
                "wip_end": counters.wip.value,
                "energy_kwh": energy,
                "specific_energy_kwh_per_part": energy / counters.completed_window
                if counters.completed_window
                else None,
                "machine": machine_metrics,
                "buffer": buffer_metrics,
            },
            "diagnostics": {
                "admitted_total": counters.admitted,
                "completed_total": counters.completed_total,
                "in_machines_end": in_machines,
                "in_buffers_end": in_buffers,
                "material_balance_error": counters.admitted
                - counters.completed_total
                - counters.wip.value,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        """Return serializable machine, buffer and elapsed-window KPI observations."""
        result = self.result()
        metrics = result["metrics"]
        return {
            "time_seconds": float(self.env.now),
            "machines": [
                {
                    "name": machine.spec.name,
                    "state": machine.state,
                    "holding_part": machine.holding_part,
                    "progress": machine.progress(),
                    "failures": machine.failures,
                }
                for machine in self.machines
            ],
            "buffers": [
                {
                    "name": buffer.spec.name,
                    "level": buffer.level.value,
                    "ready": len(buffer.ready.items),
                    "capacity": buffer.spec.capacity,
                }
                for buffer in self.buffers
            ],
            "completed_total": self.counters.completed_total,
            "completed_window": self.counters.completed_window,
            "admitted_total": self.counters.admitted,
            "wip": self.counters.wip.value,
            "material_balance_error": result["diagnostics"]["material_balance_error"],
            "observation_seconds": max(0.0, self.env.now - self.config.warmup_seconds),
            "metrics": {
                "throughput_per_hour": metrics["throughput_per_hour"],
                "average_wip": metrics["avg_wip"],
                "avg_wip": metrics["avg_wip"],
                "specific_energy_kwh_per_part": metrics["specific_energy_kwh_per_part"],
                "energy_kwh": metrics["energy_kwh"],
            },
        }

    def run(self, replication: int = 0, scenario: str = "base") -> dict[str, Any]:
        """Finish this session and return the existing batch-record structure."""
        self.advance(self.config.until_seconds)
        return self.result(replication=replication, scenario=scenario)


def run_manufacturing_replication(
    config: ManufacturingConfig,
    seed: int,
    replication: int = 0,
    scenario: str = "base",
) -> dict[str, Any]:
    """Run a serial line without altering the existing service-model simulation engine."""
    return ManufacturingSimulation(config, seed).run(replication=replication, scenario=scenario)


def _manufacturing_task(payload: dict[str, Any]) -> dict[str, Any]:
    record = run_manufacturing_replication(
        ManufacturingConfig.model_validate(payload["config"]),
        payload["seed"],
        replication=payload["replication"],
        scenario=payload["scenario"],
    )
    record["parameters"] = payload["parameters"]
    return record


def _metric_catalog(config: ManufacturingConfig) -> list[dict[str, str]]:
    specs = [
        (
            "completed",
            "context",
            "context_only",
            "parts",
            "Completed parts in the observation window.",
        ),
        (
            "throughput_per_hour",
            "primary",
            "higher_is_better",
            "parts/hour",
            "Completed parts divided by elapsed observation-window calendar hours.",
        ),
        (
            "avg_wip",
            "primary",
            "lower_is_better",
            "parts",
            "Time-weighted parts in machines and intermediate buffers; "
            "excludes raw stock and sink.",
        ),
        ("wip_end", "guardrail", "lower_is_better", "parts", "Production WIP at the horizon."),
        (
            "energy_kwh",
            "context",
            "context_only",
            "kWh",
            "All machine energy in the observation window.",
        ),
        (
            "specific_energy_kwh_per_part",
            "primary",
            "lower_is_better",
            "kWh/part",
            "Observation-window energy divided by completions; null when there are none.",
        ),
    ]
    for machine in config.machines:
        prefix = f"machine.{machine.name}"
        specs.append(
            (
                f"{prefix}.utilization",
                "driver",
                "context_only",
                "ratio",
                "Processing time divided by calendar observation time; a bottleneck proxy.",
            )
        )
        specs.append(
            (
                f"{prefix}.energy_kwh",
                "driver",
                "lower_is_better",
                "kWh",
                "Machine energy integrated over the observation window.",
            )
        )
        for state in _MachineRuntime.STATES:
            specs.append(
                (
                    f"{prefix}.state_fractions.{state}",
                    "driver",
                    "context_only",
                    "ratio",
                    f"Fraction of observation time in state {state}.",
                )
            )
    for buffer in config.buffers:
        specs.append(
            (
                f"buffer.{buffer.name}.avg_occupancy",
                "driver",
                "lower_is_better",
                "parts",
                "Time-weighted buffer occupancy, including transfer delay.",
            )
        )
    return [
        {
            "metric": metric,
            "role": role,
            "direction": direction,
            "unit": unit,
            "definition": definition,
        }
        for metric, role, direction, unit, definition in specs
    ]


def run_manufacturing_study(
    config: ManufacturingConfig | None = None,
    *,
    include_infeasible: bool = False,
    workers: int = 1,
) -> dict[str, Any]:
    """Compare independent Case A variants, paired by replication seed.

    V1 is omitted by default. An explicit include_infeasible flag permits diagnostic
    simulation of V1, while retaining every CPD violation and excluding it from
    feasible recommendation sets. No configuration is applied to the control API.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    config = ManufacturingConfig.model_validate((config or case_a_config()).model_dump())
    scenarios = case_a_scenarios(config, include_infeasible=include_infeasible)
    tasks = [
        {
            "config": variant.config.model_dump(),
            "scenario": variant.name,
            "parameters": variant.parameters,
            "replication": replication,
            "seed": derive_seed(config.base_seed, f"manufacturing:replication:{replication}"),
        }
        for variant in scenarios
        for replication in range(config.replications)
    ]
    if workers == 1:
        records = [_manufacturing_task(task) for task in tasks]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            records = list(pool.map(_manufacturing_task, tasks))
    catalog = _metric_catalog(config)
    summary = aggregate(records, config.confidence_level, metric_catalog=catalog)
    means = {(row["scenario"], row["metric"]): row["mean"] for row in summary}
    comparisons = []
    for variant in scenarios:
        metrics = {
            metric: means[variant.name, metric]
            for metric in ("throughput_per_hour", "avg_wip", "specific_energy_kwh_per_part")
        }
        changes = {}
        for metric, value in metrics.items():
            reference = means["baseline", metric]
            changes[metric] = (
                100 * (value / reference - 1)
                if value is not None and reference is not None and reference != 0
                else None
            )
        comparisons.append(
            {
                "scenario": variant.name,
                "feasible": variant.feasible,
                "means": metrics,
                "change_from_baseline_percent": changes,
            }
        )
    rankings = {
        variant.name: [
            {"rank": index, "machine": name, "utilization": utilization}
            for index, (name, utilization) in enumerate(
                sorted(
                    (
                        (machine.name, means[variant.name, f"machine.{machine.name}.utilization"])
                        for machine in config.machines
                    ),
                    key=lambda item: (-item[1], item[0]),
                ),
                start=1,
            )
        ]
        for variant in scenarios
    }
    feasible = [row for row in comparisons if row["feasible"]]
    preferred = max(feasible, key=lambda row: row["means"]["throughput_per_hour"])["scenario"]
    return {
        "schema_version": "manufacturing-1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "project_name": config.name,
        "config": config.model_dump(mode="json"),
        "assumptions": ASSUMPTIONS,
        "paper_reference": {
            "doi": "10.1016/j.jmsy.2026.02.015",
            "case": "A",
            "table": 4,
            "adaptations_figure": 7,
            "baseline_reported_means": {
                "throughput_per_hour": 7.38,
                "avg_wip": 4.47,
                "specific_energy_kwh_per_part": 14.2158,
            },
            "status": (
                "Published observations; not numerical acceptance targets for this implementation."
            ),
        },
        "random_streams": {
            "method": "blake2b_namespaced_v1",
            "common_random_numbers": True,
            "streams": "separate machine uptime and repair streams",
        },
        "scenarios": [variant.model_dump(mode="json") for variant in scenarios],
        "include_infeasible": include_infeasible,
        "metric_catalog": catalog,
        "replications": records,
        "summary": summary,
        "comparison": comparisons,
        "bottleneck_rankings": rankings,
        "evaluation": {
            "highest_mean_throughput_feasible_scenario": preferred,
            "eligible_scenarios": [row["scenario"] for row in feasible],
            "basis": "Mean throughput ranking only; review WIP and SEC trade-offs and confidence "
            "intervals. This is not a significance test or an automatic approval.",
        },
    }


def _write_manufacturing_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
            )


def save_manufacturing_study(result: dict[str, Any], output_dir: str | Path) -> Path:
    """Save auditable JSON, per-replication and summary CSVs, and a Chinese comparison."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    _write_manufacturing_csv(root / "summary.csv", result["summary"])
    _write_manufacturing_csv(
        root / "replications.csv",
        [
            {
                "scenario": record["scenario"],
                "replication": record["replication"],
                "seed": record["seed"],
                "parameters": record["parameters"],
                **flatten_numeric(record["metrics"]),
            }
            for record in result["replications"]
        ],
    )
    lines = [
        "# 论文案例一（Case A）仿真对比",
        "",
        "所有方案均从同一基准模型独立修改；相同重复编号使用共同随机数。",
        "TH 按扣除预热后的日历小时计算；WIP 为机器及中间缓冲区的时间加权平均数量。",
        "SEC 为相同统计窗口内的能耗除以完成数量；没有完成件时记为缺失。",
        "",
        "| 方案 | CPD 合规 | TH（件/小时） | 平均 WIP（件） | SEC（kWh/件） |",
        "|---|---|---:|---:|---:|",
    ]
    for row in result["comparison"]:
        metrics = row["means"]
        values = [
            metrics[key]
            for key in ("throughput_per_hour", "avg_wip", "specific_energy_kwh_per_part")
        ]
        formatted = ["缺失" if value is None else f"{value:.6f}" for value in values]
        label = "是" if row["feasible"] else "否，仅诊断，不可推荐"
        lines.append(f"| {row['scenario']} | {label} | " + " | ".join(formatted) + " |")
    lines.extend(
        [
            "",
            "V1 扩大缓冲区违反论文 CPD；仅在显式开启诊断选项时计算，始终排除在合规推荐之外。",
            "各均值的置信区间、有效重复数及标准差见 summary.csv；结果不代表生产变更已经获批。",
            "",
            "## 约束检查",
            "",
        ]
    )
    for variant in result["scenarios"]:
        findings = "; ".join(variant["violations"]) or "符合当前 Case A 约束"
        lines.append(f"- {variant['name']}: {findings}")
    lines.extend(
        [
            "",
            "## 基准瓶颈排序",
            "",
            "按实际加工时间占日历统计时间的比例排序，仅作为瓶颈候选指标。",
            "",
        ]
    )
    for item in result["bottleneck_rankings"]["baseline"]:
        lines.append(f"{item['rank']}. {item['machine']}: {item['utilization']:.2%}")
    lines.extend(["", "## 模型假设与复现范围", ""])
    lines.extend(f"- {assumption}" for assumption in result["assumptions"])
    lines.extend(
        [
            "",
            "论文报告值是外部参考，不能用来声称本实现已经获得作者软件的数值验证。",
            "本文独立实现模型，未复制作者 GPL 项目代码。",
            "",
        ]
    )
    (root / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return root
