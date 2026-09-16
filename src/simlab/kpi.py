"""KPI 采集与指标目录。

KPICollector 在仿真过程中累计时间面积积分（排队/忙碌），结束后一次性结算
系统级与工位级指标；指标目录向聚合层和 AI 分析层解释每个指标的语义与优劣方向。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


def percentile(values: list[float], probability: float) -> float | None:
    """线性插值分位数；空列表返回 None。"""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * probability
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def mean_or_none(values: list[float]) -> float | None:
    """空列表返回 None 的均值。"""

    return sum(values) / len(values) if values else None


def build_metric_catalog(station_names: list[str]) -> list[dict[str, str]]:
    """Describe the metrics that are meaningful across replications."""

    catalog = [
        {
            "metric": "arrivals",
            "role": "context",
            "direction": "context_only",
            "unit": "count",
            "definition": "统计窗口内的到达实体数。",
        },
        {
            "metric": "completed",
            "role": "context",
            "direction": "context_only",
            "unit": "count",
            "definition": "统计窗口内完成全部工位的实体数。",
        },
        {
            "metric": "throughput_per_time_unit",
            "role": "primary",
            "direction": "higher_is_better",
            "unit": "count_per_time_unit",
            "definition": "统计窗口内完成数除以窗口长度。",
        },
        {
            "metric": "wip_end",
            "role": "guardrail",
            "direction": "lower_is_better",
            "unit": "count",
            "definition": "仿真结束时仍在系统内的实体数。",
        },
        {
            "metric": "avg_cycle_time",
            "role": "driver",
            "direction": "lower_is_better",
            "unit": "time_unit",
            "definition": "统计 cohort 中已完成实体的平均总周期时间。",
        },
        {
            "metric": "p50_cycle_time",
            "role": "driver",
            "direction": "lower_is_better",
            "unit": "time_unit",
            "definition": "统计 cohort 中已完成实体周期时间的 P50。",
        },
        {
            "metric": "p95_cycle_time",
            "role": "primary",
            "direction": "lower_is_better",
            "unit": "time_unit",
            "definition": "统计 cohort 中已完成实体周期时间的 P95。",
        },
        {
            "metric": "avg_wait_time",
            "role": "driver",
            "direction": "lower_is_better",
            "unit": "time_unit",
            "definition": "所有有效工位访问的平均单次排队等待时间。",
        },
        {
            "metric": "avg_total_wait_time",
            "role": "driver",
            "direction": "lower_is_better",
            "unit": "time_unit",
            "definition": "统计 cohort 中已完成实体跨全部工位的平均总等待时间。",
        },
        {
            "metric": "service_level",
            "role": "primary",
            "direction": "higher_is_better",
            "unit": "ratio",
            "definition": "已完成 cohort 中周期时间不超过目标值的比例。",
        },
        {
            "metric": "cycle_completion_fraction",
            "role": "data_quality",
            "direction": "higher_is_better",
            "unit": "ratio",
            "definition": "统计窗口内到达的 cohort 在仿真结束前完成的比例。",
        },
        {
            "metric": "censored_cycle_count",
            "role": "data_quality",
            "direction": "lower_is_better",
            "unit": "count",
            "definition": "统计窗口内到达但在仿真结束前未完成的 cohort 实体数。",
        },
    ]
    station_metrics = (
        (
            "utilization",
            "driver",
            "target_range",
            "ratio",
            "统计窗口内忙碌资源时间占可用资源时间的比例。",
        ),
        (
            "avg_queue_length",
            "driver",
            "lower_is_better",
            "count",
            "由队长时间面积计算的平均队列长度。",
        ),
        (
            "avg_wait_time",
            "driver",
            "lower_is_better",
            "time_unit",
            "进入该工位队列的有效访问的平均等待时间。",
        ),
        (
            "p95_wait_time",
            "guardrail",
            "lower_is_better",
            "time_unit",
            "进入该工位队列的有效访问等待时间 P95。",
        ),
    )
    for station in station_names:
        for name, role, direction, unit, definition in station_metrics:
            catalog.append(
                {
                    "metric": f"station.{station}.{name}",
                    "role": role,
                    "direction": direction,
                    "unit": unit,
                    "definition": definition,
                }
            )
    return catalog


@dataclass
class KPICollector:
    """仿真过程中增量累计原始观测，finalize 时一次性结算全部 KPI。

    队列长度与利用率都基于"时间面积积分"：累计每次进入/离开事件与统计窗口
    的重叠时长，避免按离散时刻采样带来的偏差；预热期内的观测不计入统计。
    """

    warmup: float
    until: float
    station_capacities: dict[str, int]
    cycle_time_target: float | None = None
    arrivals_total: int = 0
    arrivals_window: int = 0
    completions_total: int = 0
    completions_window: int = 0
    arrival_times: dict[int, float] = field(default_factory=dict)
    active_customers: set[int] = field(default_factory=set)
    cycle_times: list[float] = field(default_factory=list)
    completed_total_waits: list[float] = field(default_factory=list)
    customer_total_waits: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    waits: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    queue_area: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    busy_area: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    open_queues: dict[tuple[int, str], float] = field(default_factory=dict)

    @property
    def window(self) -> float:
        """统计窗口长度（until - warmup）。"""

        return self.until - self.warmup

    def _overlap(self, start: float, end: float) -> float:
        # 区间与统计窗口的重叠长度；所有面积积分都裁剪到窗口内。
        return max(0.0, min(end, self.until) - max(start, self.warmup))

    def arrival(self, customer_id: int, now: float) -> None:
        """登记实体到达；统计窗口内的到达计入窗口计数。"""

        self.arrivals_total += 1
        if self.warmup <= now < self.until:
            self.arrivals_window += 1
        self.arrival_times[customer_id] = now
        self.active_customers.add(customer_id)

    def queue_enter(self, customer_id: int, station: str, now: float) -> None:
        """记录实体进入工位队列的时刻，供服务开始时计算等待时长。"""

        self.open_queues[(customer_id, station)] = now

    def service_start(self, customer_id: int, station: str, now: float) -> None:
        """服务开始：结算本次排队等待，并累计该工位的队长时间面积。"""

        queue_start = self.open_queues.pop((customer_id, station))
        self.queue_area[station] += self._overlap(queue_start, now)
        self.customer_total_waits[customer_id] += now - queue_start
        if queue_start >= self.warmup and now < self.until:
            self.waits[station].append(now - queue_start)

    def service_interval(self, station: str, start: float, end: float) -> None:
        """累计某工位忙碌区间与统计窗口重叠的时间面积。"""

        self.busy_area[station] += self._overlap(start, end)

    def completion(self, customer_id: int, now: float) -> None:
        """登记实体完成；窗口内到达且窗口内完成的进入 cohort 统计。"""

        self.completions_total += 1
        if self.warmup <= now < self.until:
            self.completions_window += 1
        arrived = self.arrival_times[customer_id]
        if arrived >= self.warmup and now < self.until:
            self.cycle_times.append(now - arrived)
            self.completed_total_waits.append(self.customer_total_waits.get(customer_id, 0.0))
        self.customer_total_waits.pop(customer_id, None)
        self.active_customers.discard(customer_id)

    def finalize(self) -> dict[str, Any]:
        """结算全部指标：工位利用率/队长/等待时间，以及系统级 KPI 与服务水平。"""

        pending_by_station: dict[str, float] = defaultdict(float)
        for (_, station), queue_start in self.open_queues.items():
            pending_by_station[station] += self._overlap(queue_start, self.until)

        station_metrics: dict[str, dict[str, float | int | None]] = {}
        all_waits: list[float] = []
        for station, capacity in self.station_capacities.items():
            waits = self.waits.get(station, [])
            all_waits.extend(waits)
            queue_area = self.queue_area.get(station, 0.0) + pending_by_station.get(station, 0.0)
            station_metrics[station] = {
                "capacity": capacity,
                "utilization": self.busy_area.get(station, 0.0) / (capacity * self.window),
                "avg_queue_length": queue_area / self.window,
                "avg_wait_time": mean_or_none(waits),
                "p95_wait_time": percentile(waits, 0.95),
                "service_starts": len(waits),
            }

        service_level = None
        if self.cycle_time_target is not None and self.cycle_times:
            service_level = sum(
                value <= self.cycle_time_target for value in self.cycle_times
            ) / len(self.cycle_times)

        cohort_arrivals = sum(
            self.warmup <= arrival < self.until for arrival in self.arrival_times.values()
        )
        censored_cycle_count = cohort_arrivals - len(self.cycle_times)
        cycle_completion_fraction = (
            len(self.cycle_times) / cohort_arrivals if cohort_arrivals else None
        )

        return {
            "arrivals": self.arrivals_window,
            "completed": self.completions_window,
            "throughput_per_time_unit": self.completions_window / self.window,
            "wip_end": len(self.active_customers),
            "avg_cycle_time": mean_or_none(self.cycle_times),
            "p50_cycle_time": percentile(self.cycle_times, 0.50),
            "p95_cycle_time": percentile(self.cycle_times, 0.95),
            "avg_wait_time": mean_or_none(all_waits),
            "avg_total_wait_time": mean_or_none(self.completed_total_waits),
            "service_level": service_level,
            "cycle_completion_fraction": cycle_completion_fraction,
            "censored_cycle_count": censored_cycle_count,
            "observed_cycle_count": len(self.cycle_times),
            "station": station_metrics,
        }
