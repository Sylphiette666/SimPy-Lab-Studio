"""事件日志挖掘：从 XES/JSONL 事件日志估计仿真模型配置（数据驱动的模型生成）。

对应目标框架的 Data collection / Data preparation / Input data 环节：
读入真实系统的事件日志（也可用本项目的 traces.xes），估计到达间隔分布、
各工位服务时间分布、工位并发容量与周期时间目标，生成可直接运行的 YAML 配置。

本模块刻意不依赖 pm4py（AGPL 许可）：XES 解析手写实现，保持 MIT 核心干净；
需要重型流程挖掘（Petri 网 / 过程树、一致性校验）时再通过可选依赖接入。

分布拟合启发式（样本量不足时结果仅供起点参考，仍需人工复核）：
- 变异系数 cv ≥ 0.9  → 指数分布（mean = 样本均值）
- cv ≤ 0.2          → 定值（value = 样本均值）
- 其余              → 三角分布（low/mode/high = 最小值/中位数/最大值）
"""
from __future__ import annotations

import json
import statistics
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from simlab.kpi import percentile

XES_NS = "http://www.xes-standard.org/"
XES_BASE_TIME = datetime(1970, 1, 1, tzinfo=UTC)
NON_ACTIVITY_EVENTS = {"arrival", "completion"}


def read_xes(path: str | Path) -> list[dict[str, Any]]:
    """读取 XES 1.0 事件日志，返回与 trace.py 写入格式一致的事件列表。"""

    root = ET.parse(path).getroot()
    events: list[dict[str, Any]] = []
    for trace in root.findall(f"{{{XES_NS}}}trace"):
        trace_attrs = _read_attributes(trace)
        case_id = trace_attrs.get("concept:name") or f"case-{len(events)}"
        for element in trace.findall(f"{{{XES_NS}}}event"):
            attrs = _read_attributes(element)
            event: dict[str, Any] = {
                "case_id": case_id,
                "activity": attrs.get("concept:name", ""),
                "lifecycle": attrs.get("lifecycle:transition", "complete"),
                "timestamp": attrs.get("simlab:time"),
            }
            resource = attrs.get("org:resource")
            if resource is not None:
                event["resource"] = resource
            extra = {
                key.removeprefix("simlab:"): value
                for key, value in attrs.items()
                if key.startswith("simlab:") and key != "simlab:time"
            }
            if extra:
                event["attributes"] = extra
            if event["timestamp"] is None:
                # 兼容不含 simlab:time 的外部日志：以 1970 基准解析 ISO 时间戳。
                raw = attrs.get("time:timestamp")
                if raw is None:
                    raise ValueError(f"event without timestamp in case {case_id}")
                stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                event["timestamp"] = (stamp - XES_BASE_TIME).total_seconds()
            else:
                event["timestamp"] = float(event["timestamp"])
            events.append(event)
    return events


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """读取 JSONL 事件日志（每行一个事件，与 trace.py 的输出格式一致）。"""

    events: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def read_log(path: str | Path) -> list[dict[str, Any]]:
    """按扩展名自动选择读取器（.xes / .jsonl）。"""

    suffix = Path(path).suffix.lower()
    if suffix == ".xes":
        return read_xes(path)
    if suffix == ".jsonl":
        return read_jsonl(path)
    raise ValueError(f"unsupported log format: {suffix or '(none)'}; expected .xes or .jsonl")


def _read_attributes(element: ET.Element) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for child in element:
        if not child.tag.startswith(f"{{{XES_NS}}}"):
            continue
        key = child.get("key")
        value = child.get("value")
        if key is None or value is None:
            continue
        if child.tag == f"{{{XES_NS}}}float":
            try:
                value = float(value)
            except ValueError:
                pass
        attributes[key] = value
    return attributes


def _fit_distribution(values: list[float]) -> dict[str, Any]:
    """按变异系数启发式挑选分布类型并估计参数。"""

    cleaned = [float(value) for value in values if value >= 0]
    if not cleaned:
        raise ValueError("cannot fit a distribution from an empty sample")
    mean = statistics.fmean(cleaned)
    if max(cleaned) - min(cleaned) < 1e-9:
        return {"kind": "deterministic", "value": round(mean, 6)}
    std = statistics.stdev(cleaned) if len(cleaned) > 1 else 0.0
    cv = std / mean if mean > 0 else 0.0
    if cv >= 0.9:
        return {"kind": "exponential", "mean": round(mean, 6)}
    if cv <= 0.2:
        return {"kind": "deterministic", "value": round(mean, 6)}
    return {
        "kind": "triangular",
        "low": round(min(cleaned), 6),
        "mode": round(statistics.median(cleaned), 6),
        "high": round(max(cleaned), 6),
    }


def estimate_model(
    events: list[dict[str, Any]],
    *,
    project_name: str = "mined-service-system",
    warmup_ratio: float = 0.1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """从事件日志估计模型，返回 (配置字典, 挖掘报告)。

    只支持“所有实体依次经过相同工位序列”的串行拓扑（与当前仿真内核一致）；
    分支 / 循环 / 并行结构会体现在报告的 warnings 中，需要人工处理。
    """

    cases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        cases[event["case_id"]].append(event)
    for case_events in cases.values():
        case_events.sort(key=lambda item: item["timestamp"])
    if not cases:
        raise ValueError("log contains no cases")

    # 场景组划分：本项目日志的 case id 形如 "<scenario>:r<rep>:<id>"，
    # 不同场景的参数/容量可能不同，容量必须按组分别计算后合并；
    # 外部日志通常不含 ":r"，整份日志视为单一场景组。
    groups: dict[str, str] = {
        case_id: case_id.split(":r", 1)[0] if ":r" in case_id else ""
        for case_id in cases
    }

    # 到达间隔：各 case 首个事件时间戳的差分（同时按组留存以检测组间差异）
    group_arrivals: dict[str, list[float]] = defaultdict(list)
    for case_id, case_events in cases.items():
        group_arrivals[groups[case_id]].append(case_events[0]["timestamp"])
    interarrivals = [
        later - earlier
        for case_events in (sorted(times) for times in group_arrivals.values())
        for earlier, later in zip(case_events, case_events[1:], strict=False)
    ]
    interarrival = _fit_distribution(interarrivals) if interarrivals else {
        "kind": "exponential",
        "mean": 5.0,
    }

    # 工位序列：取每条轨迹的工位活动首次出现顺序，按多数投票确定主导序列。
    # 只保留首次出现的活动，避免同工位重复访问（返工/循环）干扰顺序判断。
    sequences: list[tuple[str, ...]] = []
    for case_events in cases.values():
        seen: list[str] = []
        for event in case_events:
            activity = event["activity"]
            if activity in NON_ACTIVITY_EVENTS or activity in seen or not activity:
                continue
            seen.append(activity)
        if seen:
            sequences.append(tuple(seen))
    order_counter = Counter(sequences)
    dominant_order, dominant_count = order_counter.most_common(1)[0]
    coverage = dominant_count / len(sequences) if sequences else 0.0

    # 服务时间与并发容量：start → complete 配对计算时长与重叠区间数；
    # 样本按场景组分别留存，容量按组计算后取众数（平票取更小的保守值）。
    warning_counts: Counter[str] = Counter()
    durations: dict[str, list[float]] = defaultdict(list)
    group_durations: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    intervals_by_group: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for case_id, case_events in cases.items():
        group = groups[case_id]
        pending: dict[str, float] = {}
        for event in case_events:
            activity = event["activity"]
            if activity in NON_ACTIVITY_EVENTS:
                continue
            if event["lifecycle"] == "start":
                pending[activity] = event["timestamp"]
            elif event["lifecycle"] == "complete":
                start = pending.pop(activity, None)
                if start is None:
                    warning_counts[f"case 的工位 {activity} 缺少 start 事件"] += 1
                    continue
                duration = event["timestamp"] - start
                if duration >= 0:
                    durations[activity].append(duration)
                    group_durations[group][activity].append(duration)
                    intervals_by_group[group][activity].append((start, event["timestamp"]))
                else:
                    warning_counts[f"case 的工位 {activity} 时间戳乱序"] += 1
    for station in dominant_order:
        if station not in durations:
            warning_counts[f"工位 {station} 在日志中没有可用的服务时间样本"] += 1

    def _warn_group_divergence(label: str, grouped: dict[str, list[float]]) -> None:
        """组间均值差异超过 20% 时提示：合并估计只能作为起点。"""

        means = [statistics.fmean(values) for values in grouped.values() if values]
        if len(means) > 1 and statistics.fmean(means) > 0:
            if statistics.stdev(means) / statistics.fmean(means) > 0.2:
                warning_counts[f"{label}在各场景组间差异较大，合并估计仅作起点"] += 1

    _warn_group_divergence("到达间隔", {g: [t for t in ts] for g, ts in group_arrivals.items()})
    for station in dominant_order:
        _warn_group_divergence(
            f"工位 {station} 服务时间",
            {g: values.get(station, []) for g, values in group_durations.items()},
        )
    warnings = [
        f"{count} 个{message}" if count > 1 else message
        for message, count in warning_counts.most_common()
    ]

    station_configs: list[dict[str, Any]] = []
    station_stats: dict[str, Any] = {}
    for station in dominant_order:
        service_time = _fit_distribution(durations.get(station, []))
        # 容量按场景组分别计算后取众数；全部不同时取最小值（保守）。
        group_capacities = [
            _max_concurrency(intervals_by_group[group].get(station, []))
            for group in sorted(intervals_by_group)
        ] or [1]
        capacity_counts = Counter(group_capacities)
        capacity = min(group_capacities, key=lambda value: (-capacity_counts[value], value))
        station_configs.append(
            {"name": station, "capacity": capacity, "service_time": service_time}
        )
        station_stats[station] = {
            "samples": len(durations.get(station, [])),
            "mean": round(statistics.fmean(durations[station]), 4)
            if durations.get(station)
            else None,
            "capacity": capacity,
            "capacity_values": sorted(set(group_capacities)),
            "distribution": service_time["kind"],
        }

    # 周期时间与窗口：case 完成时间减到达时间；until 取最大完成时间
    cycle_times: list[float] = []
    completion_times: list[float] = []
    for case_events in cases.values():
        arrival = case_events[0]["timestamp"]
        completion = case_events[-1]["timestamp"]
        if completion > arrival:
            cycle_times.append(completion - arrival)
        completion_times.append(completion)
    until = max(completion_times)
    warmup = round(until * warmup_ratio, 6)
    if warmup >= until or warmup <= 0:
        warmup = 0.0

    if coverage < 1.0:
        warnings.append(
            f"轨迹序列不完全一致：主导序列覆盖 {coverage:.1%} 的实体，"
            "存在分支/循环/乱序，当前串行拓扑只采用主导序列"
        )

    simulation: dict[str, Any] = {
        "name": project_name,
        "until": round(until, 6),
        "warmup": warmup,
        "first_arrival_at_zero": True,
        "arrival_interarrival": interarrival,
        "stations": station_configs,
    }
    p80_cycle = percentile(cycle_times, 0.80)
    if p80_cycle is not None:
        simulation["cycle_time_target"] = round(p80_cycle, 6)

    config: dict[str, Any] = {
        "project_name": project_name,
        "simulation": simulation,
        "experiment": {
            "replications": 10,
            "base_seed": 20260825,
            "common_random_numbers": True,
            "output_dir": f"outputs/{project_name}",
        },
    }
    report: dict[str, Any] = {
        "cases": len(cases),
        "station_order": list(dominant_order),
        "order_coverage": coverage,
        "interarrival": interarrival,
        "station_stats": station_stats,
        "warnings": warnings,
    }
    return config, report


def _max_concurrency(intervals: list[tuple[float, float]]) -> int:
    """区间重叠扫描：工位同时服务的最大实体数（即挖掘出的并发容量）。"""

    if not intervals:
        return 1
    points: list[tuple[float, int]] = []
    for start, end in intervals:
        points.append((start, 1))
        points.append((end, -1))
    points.sort(key=lambda item: (item[0], item[1]))
    current = maximum = 0
    for _, delta in points:
        current += delta
        maximum = max(maximum, current)
    return max(1, maximum)


def generate_yaml(
    events: list[dict[str, Any]],
    output_path: str | Path,
    *,
    project_name: str = "mined-service-system",
    warmup_ratio: float = 0.1,
) -> tuple[Path, dict[str, Any]]:
    """挖掘并写出 YAML 配置，返回 (输出路径, 挖掘报告)。"""

    config, report = estimate_model(
        events,
        project_name=project_name,
        warmup_ratio=warmup_ratio,
    )
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target, report


__all__ = [
    "estimate_model",
    "generate_yaml",
    "read_jsonl",
    "read_log",
    "read_xes",
]
