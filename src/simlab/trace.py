"""轻量事件轨迹记录与 XES（流程挖掘标准格式）导出。

仿真内核逐实体记录事件轨迹（到达、各工位服务开始/结束、完成），
实验结束后由 ExperimentRunner.save 写入 ``traces.xes`` 与 ``traces.jsonl``。

case id 约定为 ``{scenario}:r{replication}:{customer_id}``：必须包含场景名，
否则参数网格下各场景同编号 replication 的实体会混入同一 case，导致挖掘
（mining.py）把跨场景的并发误判为一个工位的容量。

XES 时间映射约定：仿真时间被当作业务时间单位（示例配置为“分钟”），
导出时以 1970-01-01T00:00:00Z 为基准加上仿真时间偏移，形成
``time:timestamp``；原始浮点仿真时间同时保留在 ``simlab:time`` 属性中。

本模块刻意不依赖 pm4py：XES 是标准 XML，手写约百行即可保证 MIT
核心无重型第三方依赖；需要流程挖掘时再通过可选 extra 安装 pm4py。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

XES_BASE_TIME = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class TraceRecorder:
    """按实体记录事件轨迹；``enabled=False`` 时全部方法为无操作，零开销路径。"""

    enabled: bool = False
    case_prefix: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    # (case_id, station) -> queue 开始时间，用于计算等待时长
    _queue_starts: dict[tuple[str, str], float] = field(default_factory=dict)

    def _case_id(self, customer_id: int) -> str:
        return f"{self.case_prefix}{customer_id}"

    def _emit(
        self,
        case_id: str,
        activity: str,
        lifecycle: str,
        timestamp: float,
        *,
        resource: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        event: dict[str, Any] = {
            "case_id": case_id,
            "activity": activity,
            "lifecycle": lifecycle,
            "timestamp": timestamp,
        }
        if resource is not None:
            event["resource"] = resource
        if attributes:
            event["attributes"] = dict(attributes)
        self.events.append(event)

    def arrival(self, customer_id: int, now: float) -> None:
        self._emit(self._case_id(customer_id), "arrival", "complete", now)

    def queue_enter(self, customer_id: int, station: str, now: float) -> None:
        if not self.enabled:
            return
        self._queue_starts[(self._case_id(customer_id), station)] = now

    def service_start(self, customer_id: int, station: str, now: float) -> None:
        if not self.enabled:
            return
        case_id = self._case_id(customer_id)
        queue_start = self._queue_starts.pop((case_id, station), now)
        self._emit(
            case_id,
            station,
            "start",
            now,
            resource=station,
            attributes={"wait_time": round(now - queue_start, 9)},
        )

    def service_end(
        self,
        customer_id: int,
        station: str,
        now: float,
        service_time: float,
    ) -> None:
        self._emit(
            self._case_id(customer_id),
            station,
            "complete",
            now,
            resource=station,
            attributes={"service_time": round(service_time, 9)},
        )

    def completion(self, customer_id: int, now: float) -> None:
        self._emit(self._case_id(customer_id), "completion", "complete", now)


def _iso_timestamp(sim_time: float) -> str:
    """仿真时间 -> ISO 8601 时间戳（基准时间 + 偏移，单位沿用业务时间单位）。"""

    stamp = XES_BASE_TIME + timedelta(seconds=float(sim_time))
    return stamp.isoformat(timespec="microseconds")


def write_xes(
    events: list[dict[str, Any]],
    path: str | Path,
    *,
    log_name: str = "simlab-trace-log",
) -> Path:
    """把事件列表写成极简标准 XES 文件（按 case 分组，按时间排序）。

    事件约定：``activity`` 为工位名或 arrival/completion；``lifecycle`` 为
    start/complete；``resource`` 为工位名；``attributes`` 为附加数值属性。
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    traces: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        traces.setdefault(event["case_id"], []).append(event)
    for case_events in traces.values():
        case_events.sort(key=lambda item: item["timestamp"])

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<log xes.version="1.0" xes.features="nested-attributes" '
        'xmlns="http://www.xes-standard.org/">',
        f"  <string key={quoteattr('concept:name')} value={quoteattr(log_name)}/>",
        f"  <string key={quoteattr('simlab:source')} value={quoteattr('simlab-trace-recorder')}/>",
    ]
    for case_id in sorted(traces):
        lines.append("  <trace>")
        lines.append(f"    <string key={quoteattr('concept:name')} value={quoteattr(case_id)}/>")
        for event in traces[case_id]:
            lines.append("    <event>")
            lines.append(
                f"      <string key={quoteattr('concept:name')} "
                f"value={quoteattr(event['activity'])}/>"
            )
            lines.append(
                f"      <string key={quoteattr('lifecycle:transition')} "
                f"value={quoteattr(event['lifecycle'])}/>"
            )
            lines.append(
                f"      <date key={quoteattr('time:timestamp')} "
                f"value={quoteattr(_iso_timestamp(event['timestamp']))}/>"
            )
            resource = event.get("resource")
            if resource is not None:
                lines.append(
                    f"      <string key={quoteattr('org:resource')} "
                    f"value={quoteattr(resource)}/>"
                )
            lines.append(
                f"      <float key={quoteattr('simlab:time')} "
                f"value={quoteattr(repr(event['timestamp']))}/>"
            )
            for key, value in sorted(event.get("attributes", {}).items()):
                lines.append(
                    f"      <float key={quoteattr(f'simlab:{key}')} "
                    f"value={quoteattr(repr(value))}/>"
                )
            lines.append("    </event>")
        lines.append("  </trace>")
    lines.append("</log>")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def write_jsonl(events: list[dict[str, Any]], path: str | Path) -> Path:
    """把事件列表写成 JSONL 文件，便于 pandas / polars 直接读取分析。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(events, key=lambda item: (item["case_id"], item["timestamp"]))
    with target.open("w", encoding="utf-8") as stream:
        for event in ordered:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return target


__all__ = ["TraceRecorder", "write_jsonl", "write_xes"]
