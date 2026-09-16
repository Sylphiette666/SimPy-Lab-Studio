from __future__ import annotations

import random
from typing import Any

import simpy

from simlab.config import SimulationConfig
from simlab.kpi import KPICollector
from simlab.rng import derive_seed
from simlab.trace import TraceRecorder


def run_replication(
    simulation: SimulationConfig,
    seed: int,
    replication: int = 0,
    scenario: str = "base",
    parameters: dict[str, Any] | None = None,
    record_traces: bool = False,
) -> dict[str, Any]:
    """Run one deterministic replication for a given seed.

    ``record_traces=True`` 时逐实体记录事件轨迹（见 trace.TraceRecorder），
    返回结果中携带 ``trace_events``；关闭时该键为空列表且几乎无额外开销。
    """

    arrival_rng = random.Random(derive_seed(seed, "arrivals"))
    station_rngs = {
        station.name: random.Random(derive_seed(seed, f"service:{station.name}"))
        for station in simulation.stations
    }
    env = simpy.Environment()
    resources = {
        station.name: simpy.Resource(env, capacity=station.capacity)
        for station in simulation.stations
    }
    collector = KPICollector(
        warmup=simulation.warmup,
        until=simulation.until,
        station_capacities={station.name: station.capacity for station in simulation.stations},
        cycle_time_target=simulation.cycle_time_target,
    )
    recorder = TraceRecorder(
        enabled=record_traces,
        # 前缀必须含场景名：参数网格下各场景的 replication 编号相同，
        # 不带场景名会导致跨场景实体轨迹混入同一 case。
        case_prefix=f"{scenario}:r{replication}:",
    )

    def customer(customer_id: int):
        collector.arrival(customer_id, env.now)
        recorder.arrival(customer_id, env.now)
        for station in simulation.stations:
            collector.queue_enter(customer_id, station.name, env.now)
            recorder.queue_enter(customer_id, station.name, env.now)
            with resources[station.name].request() as request:
                yield request
                collector.service_start(customer_id, station.name, env.now)
                recorder.service_start(customer_id, station.name, env.now)
                duration = station.service_time.sample(station_rngs[station.name])
                collector.service_interval(station.name, env.now, env.now + duration)
                yield env.timeout(duration)
                recorder.service_end(customer_id, station.name, env.now, duration)
        collector.completion(customer_id, env.now)
        recorder.completion(customer_id, env.now)

    def arrivals():
        customer_id = 0
        if not simulation.first_arrival_at_zero:
            delay = simulation.arrival_interarrival.sample(arrival_rng)
            if delay >= simulation.until:
                return
            yield env.timeout(delay)

        while env.now < simulation.until:
            if simulation.max_arrivals is not None and customer_id >= simulation.max_arrivals:
                return
            env.process(customer(customer_id))
            customer_id += 1
            delay = simulation.arrival_interarrival.sample(arrival_rng)
            if env.now + delay >= simulation.until:
                return
            yield env.timeout(delay)

    env.process(arrivals())
    env.run(until=simulation.until)

    return {
        "scenario": scenario,
        "parameters": parameters or {},
        "replication": replication,
        "seed": seed,
        "metrics": collector.finalize(),
        "trace_events": recorder.events,
    }


def run_replication_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Pickle-friendly adapter used by ProcessPoolExecutor."""

    simulation = SimulationConfig.model_validate(payload["simulation"])
    return run_replication(
        simulation=simulation,
        seed=payload["seed"],
        replication=payload["replication"],
        scenario=payload["scenario"],
        parameters=payload["parameters"],
        record_traces=payload.get("record_traces", False),
    )
