"""结果可视化：KPI 对比、工位利用率、轨迹甘特图与模型结构图（PNG + HTML 报告）。

对应目标框架的 Model visualization / Model comparison / Decision support 环节。
配色遵循数据可视化规范：分类色使用验证过的固定顺序调色板（前 3 色满足
全组合色盲可辨），浅色表面 + 细标记 + 直接标注 + 弱化轴线；每个子图只画
一个量纲（绝不使用双轴）。输出为静态 PNG（Agg 后端）与轻量 HTML 报告。
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (Agg 必须先于 pyplot 导入)

from simlab.config import SimulationConfig  # noqa: E402

# 验证过的浅色表面调色板（dataviz 参考实例，分类槽位固定顺序，勿随意换序）
_CATEGORICAL = [
    "#2a78d6",  # slot 1 blue
    "#eb6834",  # slot 2 orange
    "#1baf7a",  # slot 3 aqua
    "#eda100",  # slot 4 yellow
    "#e87ba4",  # slot 5 magenta
    "#008300",  # slot 6 green
    "#4a3aa7",  # slot 7 violet
    "#e34948",  # slot 8 red
]
_SURFACE = "#fcfcfb"
_INK = "#0b0b0b"
_SECONDARY = "#52514e"
_MUTED = "#898781"
_GRID = "#e1e0d9"
_AXIS = "#c3c2b7"

KEY_METRICS = [
    ("avg_cycle_time", "平均周期时间"),
    ("p95_cycle_time", "P95 周期时间"),
    ("avg_wait_time", "平均等待时间"),
    ("service_level", "服务水平"),
    ("throughput_per_time_unit", "吞吐率"),
]


def _setup_style() -> None:
    """注册中文字体并应用规范化的图表外观。"""

    from matplotlib import font_manager, rcParams

    for font_path in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ):
        if Path(font_path).exists():
            try:
                font_manager.fontManager.addfont(font_path)
            except RuntimeError:
                pass
    rcParams["font.family"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
    rcParams["axes.unicode_minus"] = False
    rcParams["figure.facecolor"] = _SURFACE
    rcParams["axes.facecolor"] = _SURFACE
    rcParams["text.color"] = _INK
    rcParams["axes.labelcolor"] = _SECONDARY
    rcParams["axes.edgecolor"] = _AXIS
    rcParams["axes.linewidth"] = 0.8
    rcParams["xtick.color"] = _MUTED
    rcParams["ytick.color"] = _MUTED
    rcParams["grid.color"] = _GRID
    rcParams["grid.linewidth"] = 0.8
    rcParams["savefig.dpi"] = 150
    rcParams["savefig.facecolor"] = _SURFACE


def _scenario_colors(scenarios: list[str]) -> dict[str, str]:
    """场景名 → 固定顺序的分类色；场景与颜色绑定后不随筛选/排序改变。"""

    return {
        scenario: _CATEGORICAL[index % len(_CATEGORICAL)]
        for index, scenario in enumerate(scenarios)
    }


# 在导入时完成中文字体注册与外观设置：单个绘图函数被直接调用
# （不经 plot_results）时也必须生效，否则中文会回退到 DejaVu 显示为方框。
_setup_style()


def _style_axes(axes: Any) -> None:
    """弱化轴线：只保留左侧与底边，网格用发丝线。"""

    for spine in ("top", "right"):
        axes.spines[spine].set_visible(False)
    axes.grid(axis="y", linewidth=0.8)
    axes.tick_params(length=0)


def _summary_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result.get("summary", [])


def plot_kpi_comparison(result: dict[str, Any], output_dir: str | Path) -> Path:
    """小倍图：每个关键 KPI 一个子图，各场景带 95% CI 误差棒的细柱。"""

    rows = _summary_rows(result)
    scenarios = sorted({row["scenario"] for row in rows})
    colors = _scenario_colors(scenarios)
    figure, axes = plt.subplots(2, 3, figsize=(11, 6))
    flat_axes = list(axes.flat)
    for index, (metric, label) in enumerate(KEY_METRICS):
        axis = flat_axes[index]
        values: dict[str, tuple[float | None, float | None, float | None]] = {}
        for row in rows:
            if row["metric"] == metric:
                values[row["scenario"]] = (row["mean"], row["ci_low"], row["ci_high"])
        x = range(len(scenarios))
        means = [values[s][0] for s in scenarios]
        low_errors = []
        high_errors = []
        for scenario in scenarios:
            mean, low, high = values[scenario]
            if mean is None or low is None or high is None:
                low_errors.append(0.0)
                high_errors.append(0.0)
            else:
                low_errors.append(mean - low)
                high_errors.append(high - mean)
        bars = axis.bar(
            x,
            [value if value is not None else 0.0 for value in means],
            width=0.55,
            color=[colors[s] for s in scenarios],
            yerr=[low_errors, high_errors],
            capsize=3,
            error_kw={"linewidth": 0.8, "ecolor": _MUTED},
            zorder=3,
        )
        for bar, scenario in zip(bars, scenarios, strict=True):
            value = values[scenario][0]
            if value is None:
                continue
            axis.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height(),
                f"{value:.2f}",
                ha="center",
                va="bottom",
                fontsize=7,
                color=_SECONDARY,
            )
        axis.set_title(label, fontsize=9, color=_INK, pad=6)
        axis.set_xticks(list(x), [s.replace("__", "\n") for s in scenarios], fontsize=7)
        _style_axes(axis)
    flat_axes[-1].axis("off")
    figure.suptitle("KPI 场景对比（误差棒为 95% 置信区间）", fontsize=11, color=_INK, y=0.99)
    figure.tight_layout(rect=(0, 0, 1, 0.96))
    path = Path(output_dir) / "kpi_comparison.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path)
    plt.close(figure)
    return path


def plot_station_utilization(result: dict[str, Any], output_dir: str | Path) -> Path:
    """各工位利用率按场景分组对比（组内细柱 + 2px 表面间隙）。"""

    rows = _summary_rows(result)
    scenarios = sorted({row["scenario"] for row in rows})
    stations = sorted(
        {
            row["metric"].removeprefix("station.").removesuffix(".utilization")
            for row in rows
            if row["metric"].endswith(".utilization")
        }
    )
    colors = _scenario_colors(scenarios)
    figure, axis = plt.subplots(figsize=(8, 4.5))
    x = range(len(stations))
    width = min(0.72 / max(len(scenarios), 1), 0.3)
    for offset, scenario in enumerate(scenarios):
        values: list[float | None] = []
        for station in stations:
            match = [
                row["mean"]
                for row in rows
                if row["scenario"] == scenario
                and row["metric"] == f"station.{station}.utilization"
            ]
            values.append(match[0] if match else None)
        positions = [position + (offset - (len(scenarios) - 1) / 2) * width for position in x]
        axis.bar(
            positions,
            [value if value is not None else 0.0 for value in values],
            width=width,
            label=scenario,
            color=colors[scenario],
            zorder=3,
        )
        for position, value in zip(positions, values, strict=True):
            if value is not None:
                axis.text(
                    position,
                    value,
                    f"{value:.2f}",
                    ha="center",
                    va="bottom",
                    fontsize=6.5,
                    color=_SECONDARY,
                )
    axis.set_xticks(list(x), stations, fontsize=8)
    axis.set_ylabel("利用率", fontsize=8)
    axis.set_ylim(0, 1.15)
    axis.legend(fontsize=8, frameon=False)
    _style_axes(axis)
    axis.set_title("工位利用率对比", fontsize=10, color=_INK, pad=8)
    figure.tight_layout()
    path = Path(output_dir) / "station_utilization.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path)
    plt.close(figure)
    return path


def plot_trace_gantt(
    result: dict[str, Any],
    output_dir: str | Path,
    *,
    max_cases: int = 12,
) -> Path | None:
    """取前若干个实体的轨迹画甘特图：各工位服务区间按时间轴展开。"""

    events = result.get("trace_events") or []
    if not events:
        return None
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_case[event["case_id"]].append(event)
    case_ids = sorted(by_case)[:max_cases]
    stations = sorted(
        {
            event["activity"]
            for event in events
            if event["activity"] not in {"arrival", "completion"}
        }
    )
    station_colors = {
        station: _CATEGORICAL[index % len(_CATEGORICAL)]
        for index, station in enumerate(stations)
    }
    figure, axis = plt.subplots(figsize=(9, 0.32 * len(case_ids) + 1.6))
    for row, case_id in enumerate(case_ids):
        for event in by_case[case_id]:
            if event["lifecycle"] != "start":
                continue
            activity = event["activity"]
            if activity in {"arrival", "completion"}:
                continue
            end_event = next(
                (
                    other
                    for other in by_case[case_id]
                    if other["activity"] == activity
                    and other["lifecycle"] == "complete"
                    and other["timestamp"] >= event["timestamp"]
                ),
                None,
            )
            if end_event is None:
                continue
            axis.barh(
                row,
                end_event["timestamp"] - event["timestamp"],
                left=event["timestamp"],
                height=0.55,
                color=station_colors[activity],
                edgecolor="none",
                zorder=3,
            )
    axis.set_yticks(range(len(case_ids)), case_ids, fontsize=7)
    axis.invert_yaxis()
    axis.set_xlabel("仿真时间", fontsize=8)
    axis.set_title(f"实体轨迹甘特图（前 {len(case_ids)} 个实体）", fontsize=10, color=_INK, pad=8)
    _style_axes(axis)
    handles = [
        plt.Rectangle((0, 0), 1, 1, color=station_colors[station]) for station in stations
    ]
    axis.legend(handles, stations, fontsize=7, frameon=False, ncol=min(len(stations), 4))
    figure.tight_layout()
    path = Path(output_dir) / "traces_gantt.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path)
    plt.close(figure)
    return path


def plot_model_structure(result: dict[str, Any], output_dir: str | Path) -> Path:
    """模型结构图：到达源 → 各工位（容量/分布）→ 完成汇的横向框图。"""

    simulation = SimulationConfig.model_validate(result["config"]["simulation"])
    boxes = ["到达"] + [
        f"{station.name}\n容量 {station.capacity}\n服务 {station.service_time.kind}"
        for station in simulation.stations
    ] + ["完成"]
    figure, axis = plt.subplots(figsize=(len(boxes) * 2.1 + 1, 2.8))
    width, height = 0.56, 0.42
    positions = list(range(len(boxes)))
    for position, text in zip(positions, boxes, strict=True):
        axis.add_patch(
            plt.Rectangle(
                (position - width / 2, -height / 2),
                width,
                height,
                facecolor="#eef6fb",
                edgecolor=_CATEGORICAL[0],
                linewidth=1.2,
                zorder=3,
            )
        )
        axis.text(position, 0, text, ha="center", va="center", fontsize=7.5, color=_INK)
    for left, right in zip(positions[:-1], positions[1:], strict=True):
        axis.annotate(
            "",
            xy=(right - width / 2, 0),
            xytext=(left + width / 2, 0),
            arrowprops={"arrowstyle": "-|>", "color": _AXIS, "lw": 1.4},
        )
    axis.set_xlim(-1.1, len(boxes) - 0.1)
    axis.set_ylim(-0.75, 0.75)
    axis.axis("off")
    axis.set_title(
        f"模型结构：{simulation.name}（until={simulation.until}，warmup={simulation.warmup}）",
        fontsize=10,
        color=_INK,
        pad=8,
    )
    figure.tight_layout()
    path = Path(output_dir) / "model_structure.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path)
    plt.close(figure)
    return path


def _report_table_html(result: dict[str, Any]) -> str:
    rows = _summary_rows(result)
    scenarios = sorted({row["scenario"] for row in rows})
    lines = ["<table>", "<tr><th>KPI</th>" + "".join(
        f"<th>{html.escape(scenario)}</th>" for scenario in scenarios
    ) + "</tr>"]
    for metric, label in KEY_METRICS:
        cells = []
        for scenario in scenarios:
            match = [
                row
                for row in rows
                if row["scenario"] == scenario and row["metric"] == metric
            ]
            if not match or match[0]["mean"] is None:
                cells.append("<td>—</td>")
                continue
            row = match[0]
            cells.append(
                f"<td>{row['mean']:.3f}<br><small>[{row['ci_low']:.3f}, "
                f"{row['ci_high']:.3f}]</small></td>"
            )
        lines.append(f"<tr><td>{label}</td>{''.join(cells)}</tr>")
    lines.append("</table>")
    return "\n".join(lines)


def plot_results(
    result: dict[str, Any],
    output_dir: str | Path,
    *,
    max_cases: int = 12,
) -> list[Path]:
    """生成全部图表与 HTML 报告，返回产物文件列表。"""

    _setup_style()
    output = Path(output_dir)
    outputs = [
        plot_kpi_comparison(result, output),
        plot_station_utilization(result, output),
        plot_model_structure(result, output),
    ]
    gantt = plot_trace_gantt(result, output, max_cases=max_cases)
    if gantt is not None:
        outputs.append(gantt)

    report_path = output / "report.html"
    image_blocks = "\n".join(
        f'<h2>{image.stem.replace("_", " ")}</h2><img src="{image.name}" '
        f'style="max-width:100%;border:1px solid #e1e0d9;">'
        for image in outputs
    )
    css = (
        "body{font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;"
        "background:#f9f9f7;color:#0b0b0b;max-width:960px;margin:0 auto;padding:24px;}"
        "h1{color:#0b0b0b}h2{margin-top:32px;color:#52514e}"
        "table{border-collapse:collapse;width:100%;margin:16px 0}"
        "th,td{border:1px solid #c3c2b7;padding:6px 10px;text-align:left;font-size:14px}"
        "th{background:#eef6fb}"
    )
    report_path.write_text(
        "\n".join(
            [
                '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">',
                f"<title>仿真结果报告：{html.escape(result.get('project_name', 'simlab'))}</title>",
                f"<style>{css}</style></head><body>",
                f"<h1>仿真结果报告：{html.escape(result.get('project_name', 'simlab'))}</h1>",
                _report_table_html(result),
                image_blocks,
                "</body></html>",
            ]
        ),
        encoding="utf-8",
    )
    outputs.append(report_path)
    return outputs


def load_results(path: str | Path) -> dict[str, Any]:
    """读取 results.json（可选：把同目录 traces.jsonl 合并进 trace_events）。"""

    results_path = Path(path)
    result = json.loads(results_path.read_text(encoding="utf-8"))
    if not result.get("trace_events"):
        traces_path = results_path.with_name("traces.jsonl")
        if traces_path.exists():
            result["trace_events"] = [
                json.loads(line)
                for line in traces_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
    return result


__all__ = [
    "load_results",
    "plot_kpi_comparison",
    "plot_model_structure",
    "plot_results",
    "plot_station_utilization",
    "plot_trace_gantt",
]
