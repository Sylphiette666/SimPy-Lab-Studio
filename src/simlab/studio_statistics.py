"""Study diagnostics from independent replications, never sampled animation frames.

Student-t mean and paired-difference intervals follow NIST e-Handbook 7.3.1.1.
They assume independent replication differences with approximately normal means.
"""

from __future__ import annotations

import math
import statistics
from functools import lru_cache

METRICS = {
    "throughput_per_hour": ("产出率", "件/小时", 1),
    "avg_wip": ("平均在制品", "件", -1),
    "specific_energy_kwh_per_part": ("单位产品能耗", "kWh/件", -1),
}


def _beta_fraction(a, b, x):
    tiny = 1e-300
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 301):
        for aa in (
            m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
            -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1)),
        ):
            d, c = 1 + aa * d, 1 + aa / c
            d = 1 / (d if abs(d) > tiny else tiny)
            c = c if abs(c) > tiny else tiny
            delta = d * c
            h *= delta
        if abs(delta - 1) < 1e-13:
            return h
    raise ArithmeticError("beta fraction did not converge")


def t_two_sided_p(t, df):
    if df < 1:
        raise ValueError("positive degrees of freedom required")
    x, a, b = df / (df + t * t), df / 2, 0.5
    if x >= 1:
        return 1.0
    if x <= 0:
        return 0.0
    factor = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    value = (
        factor * _beta_fraction(a, b, x) / a
        if x < (a + 1) / (a + b + 2)
        else 1 - factor * _beta_fraction(b, a, 1 - x) / b
    )
    return min(1.0, max(0.0, value))


@lru_cache(maxsize=2048)
def t_critical(confidence, df):
    if not 0 < confidence < 1 or df < 1:
        raise ValueError("invalid confidence or degrees of freedom")
    low, high = 0.0, 1.0
    while t_two_sided_p(high, df) > 1 - confidence:
        high *= 2
    for _ in range(65):
        middle = (low + high) / 2
        if t_two_sided_p(middle, df) > 1 - confidence:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def interval(values, confidence=0.95):
    values = [
        v
        for v in values
        if isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v)
    ]
    n = len(values)
    mean = statistics.fmean(values) if n else None
    std = statistics.stdev(values) if n > 1 else None
    margin = t_critical(confidence, n - 1) * std / math.sqrt(n) if n > 1 else None
    return {
        "n": n,
        "mean": mean,
        "std": std,
        "half_width": margin,
        "ci_low": mean - margin if margin is not None else None,
        "ci_high": mean + margin if margin is not None else None,
        "confidence_level": confidence,
        "ci_method": "student_t",
    }


def analyze_study(study, baseline=None, relative_precision=0.05):
    records = study["replications"]
    confidence = study["config"].get("confidence_level", 0.95)
    rows = []
    comparable = bool(baseline) and all(
        study["config"].get(key) == baseline["config"].get(key)
        for key in ("until_seconds", "warmup_seconds", "base_seed", "confidence_level")
    )
    for metric, (label, unit, direction) in METRICS.items():
        row = {
            "metric": metric,
            "label": label,
            "unit": unit,
            **interval([r["metrics"].get(metric) for r in records], confidence),
        }
        required = None
        if row["n"] > 1 and row["mean"] and row["std"] is not None:
            target = abs(row["mean"]) * relative_precision
            required = next(
                (
                    n
                    for n in range(max(2, row["n"]), 51)
                    if t_critical(confidence, n - 1) * row["std"] / math.sqrt(n) <= target
                ),
                51,
            )
        row.update(
            recommended_replications=required,
            target_relative_half_width=relative_precision,
            missing=len(records) - row["n"],
        )
        if comparable:
            control = {
                (r["replication"], r["seed"]): r["metrics"].get(metric)
                for r in baseline["replications"]
            }
            differences = []
            for r in records:
                current, previous = (
                    r["metrics"].get(metric),
                    control.get((r["replication"], r["seed"])),
                )
                if all(
                    isinstance(v, (int, float)) and math.isfinite(v) for v in (current, previous)
                ):
                    differences.append(current - previous)
            paired = interval(differences, confidence)
            p = None
            if paired["n"] > 1 and paired["std"] > 0:
                p = t_two_sided_p(
                    paired["mean"] / (paired["std"] / math.sqrt(paired["n"])), paired["n"] - 1
                )
            # Zero observed variance cannot establish population certainty.
            significant = p is not None and p < 1 - confidence
            paired.update(
                p_value=p,
                significant=significant,
                conclusion=("改善" if paired["mean"] * direction > 0 else "退化")
                if significant
                else "证据不足",
            )
            row["paired"] = paired
        rows.append(row)
    machines, buffers = [], []
    states = ["processing", "blocked", "starved", "failed", "off_shift"]
    for spec in study["config"]["machines"]:
        samples = [r["metrics"].get("machine", {}).get(spec["name"], {}) for r in records]
        fractions = (
            {
                key: statistics.fmean(s.get("state_fractions", {}).get(key, 0) for s in samples)
                for key in states
            }
            if samples
            else {}
        )
        machines.append(
            {
                "name": spec["name"],
                "fractions": fractions,
                "seconds": {
                    key: statistics.fmean(s.get("state_seconds", {}).get(key, 0) for s in samples)
                    for key in states
                }
                if samples
                else {},
            }
        )
    for spec in study["config"]["buffers"]:
        samples = [r["metrics"].get("buffer", {}).get(spec["name"], {}) for r in records]
        fill = [s.get("avg_occupancy", 0) / spec["capacity"] for s in samples]
        buffers.append(
            {
                "name": spec["name"],
                "capacity": spec["capacity"],
                "mean_fill": statistics.fmean(fill) if fill else 0,
                "replication_fill": fill,
            }
        )
    return {
        "metrics": rows,
        "machines": machines,
        "buffers": buffers,
        "comparable": comparable,
        "confidence_level": confidence,
        "note": "基于独立重复，t 区间假设重复均值近似正态；配对按重复编号和种子匹配。"
        "多指标检验未校正，适合探索，不是最优性证明。零方差不等于没有不确定性。"
        "重复次数建议固定当前方差估计，超过 50 次时需调整实验设计。",
    }
