"""Sampled production-line replay and arbitrary-configuration experiments.

Frames are observations of actual SimPy state. They are not a synthetic animation
or a log of every material movement. Frontends can pause, scrub and change replay
speed without changing the underlying experiment or its random streams.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from simlab.experiment import aggregate
from simlab.manufacturing import (
    ASSUMPTIONS,
    ManufacturingConfig,
    ManufacturingSimulation,
    _metric_catalog,
    run_manufacturing_replication,
)
from simlab.rng import derive_seed


def _replication_seed(config: ManufacturingConfig, replication: int) -> int:
    return derive_seed(config.base_seed, f"manufacturing:replication:{replication}")


def generate_preview(
    config: ManufacturingConfig,
    *,
    seed: int | None = None,
    max_frames: int = 600,
    preview_seconds: float | None = None,
) -> dict[str, Any]:
    """Return at most ``max_frames + 1`` snapshots including both endpoints.

    By default the entire configured horizon is simulated and sampled. An explicit
    shorter ``preview_seconds`` limits the simulated horizon and marks the result
    as truncated. KPI denominators use elapsed post-warmup time, even in a short
    preview, and pre-warmup observations report zero rates and null SEC.

    The default seed is the study's first replication seed. Thus the last frame of
    a full preview can be compared directly with that replication's final KPIs.
    """
    config = ManufacturingConfig.model_validate(config.model_dump())
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames < 1:
        raise ValueError("max_frames must be a positive integer")
    if preview_seconds is not None and (
        isinstance(preview_seconds, bool)
        or not math.isfinite(preview_seconds)
        or preview_seconds <= 0
    ):
        raise ValueError("preview_seconds must be a finite positive number")
    if seed is None:
        seed = _replication_seed(config, 0)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    duration = min(config.until_seconds, preview_seconds or config.until_seconds)
    simulation = ManufacturingSimulation(config, seed)
    frames = [simulation.snapshot()]
    for index in range(1, max_frames + 1):
        # Pin the last sample to the exact horizon; floating-point multiplication
        # must not create a point just beyond the configured endpoint.
        time_seconds = duration if index == max_frames else duration * index / max_frames
        frames.append(simulation.advance(time_seconds))
    return {
        "frames": frames,
        "duration_seconds": duration,
        "configured_duration_seconds": config.until_seconds,
        "seed": seed,
        "warmup_seconds": config.warmup_seconds,
        "truncated": duration < config.until_seconds,
        "sampling_interval_seconds": duration / max_frames,
        "description": (
            "真实离散事件仿真的定时状态采样；播放速度只改变回放速度。"
            "采样之间可能发生多次加工、转移和故障，画面不逐件展示全部事件。"
            "指标仅统计当前已运行的预热后时段；预热结束前单位能耗为空。"
            "修改模型后从初始状态重新仿真，不在当前回放时刻热修改。"
        ),
    }


def run_config_study(config: ManufacturingConfig) -> dict[str, Any]:
    """Evaluate exactly this serial-line configuration for its requested replications.

    This function deliberately does not inject the paper's fixed V1/V2/V3 variants
    or impose Case A constraints. The studio's selected experiment mode owns those
    constraints. Equal base seeds and replication indices across model versions
    give common random numbers for comparison.
    """
    config = ManufacturingConfig.model_validate(config.model_dump())
    records = [
        run_manufacturing_replication(
            config,
            _replication_seed(config, replication),
            replication=replication,
            scenario="current",
        )
        for replication in range(config.replications)
    ]
    catalog = _metric_catalog(config)
    summary = aggregate(records, config.confidence_level, metric_catalog=catalog)
    return {
        "schema_version": "manufacturing-studio-1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "project_name": config.name,
        "config": config.model_dump(mode="json"),
        "assumptions": [
            assumption
            for assumption in ASSUMPTIONS
            if not assumption.startswith("The Case A process")
        ],
        "random_streams": {
            "method": "blake2b_namespaced_v1",
            "common_random_numbers": True,
            "streams": "separate machine uptime and repair streams",
        },
        "metric_catalog": catalog,
        "replications": records,
        "summary": summary,
    }
