"""服务端安全策略：动作 allowlist、配置变更落地与工作量上限校验。

AI 提案必须先通过 allowed_actions 清单才能进入工作流；每项变更落地后
重新整体校验配置，并估算事件总量，防止单个请求拖垮服务器。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from simlab.agent import AllowedAction, ProposedAction
from simlab.config import ProjectConfig, SimulationConfig, StationConfig
from simlab.workflow import (
    Action,
    ChangePolicyAction,
    EnableResourceAction,
    SetParameterAction,
)


class ActionPolicyError(ValueError):
    """Raised when a declarative action is outside the local safety policy."""


class WorkloadLimitError(ActionPolicyError):
    """Raised when a valid config would create an unsafe server workload."""


class PolicyLimits(BaseModel):
    """服务端资源上限；超限的合法配置会被拒绝。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_replications: int = Field(default=500, ge=1)
    max_scenarios: int = Field(default=64, ge=1)
    max_total_replications: int = Field(default=2_000, ge=1)
    max_until: float = Field(default=1_000_000, gt=0)
    max_stations: int = Field(default=50, ge=1)
    max_arrivals_per_replication: int = Field(default=100_000, ge=1)
    max_estimated_events: int = Field(default=10_000_000, ge=1)


def canonical_config_hash(config: ProjectConfig) -> str:
    """配置的规范 JSON 哈希，用作"提案基于哪版配置"的指纹。"""

    payload = json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def resource_catalog(config: ProjectConfig) -> dict[str, StationConfig]:
    """按名称快照工位定义；工位被停用后仍可按原定义恢复。"""

    return {station.name: station.model_copy(deep=True) for station in config.simulation.stations}


def allowed_actions(config: ProjectConfig) -> list[AllowedAction]:
    """Return the exact action/target pairs an AI may propose for this config."""

    actions = [
        AllowedAction(
            action_type="set_parameter",
            target="simulation.until",
            description="调整下一轮仿真的结束时间；仍受服务端上限和 warmup 校验约束。",
        ),
        AllowedAction(
            action_type="set_parameter",
            target="simulation.warmup",
            description="调整下一轮仿真的预热期。",
        ),
        AllowedAction(
            action_type="set_parameter",
            target="simulation.max_arrivals",
            description="调整下一轮最多到达实体数，可设为 null。",
        ),
        AllowedAction(
            action_type="set_parameter",
            target="simulation.cycle_time_target",
            description="调整服务水平使用的周期时间目标，可设为 null。",
        ),
        AllowedAction(
            action_type="set_parameter",
            target="experiment.replications",
            description="调整下一轮每个场景的重复次数，受总工作量上限约束。",
        ),
        AllowedAction(
            action_type="set_parameter",
            target="experiment.confidence_level",
            description="调整下一轮统计置信水平。",
        ),
        AllowedAction(
            action_type="change_policy",
            target="experiment.common_random_numbers",
            description="用布尔值切换共同随机数策略。",
        ),
        AllowedAction(
            action_type="change_policy",
            target="simulation.first_arrival_at_zero",
            description="用布尔值切换首次到达是否发生在零时刻。",
        ),
    ]

    arrival = config.simulation.arrival_interarrival
    for field_name in ("mean", "value", "low", "high", "mode"):
        if getattr(arrival, field_name) is not None:
            actions.append(
                AllowedAction(
                    action_type="set_parameter",
                    target=f"simulation.arrival_interarrival.{field_name}",
                    description=f"调整到达间隔分布的 {field_name} 参数。",
                )
            )

    for index, station in enumerate(config.simulation.stations):
        actions.append(
            AllowedAction(
                action_type="set_parameter",
                target=f"simulation.stations.{index}.capacity",
                description=f"调整工位 {station.name} 的容量。",
            )
        )
        for field_name in ("mean", "value", "low", "high", "mode"):
            if getattr(station.service_time, field_name) is not None:
                actions.append(
                    AllowedAction(
                        action_type="set_parameter",
                        target=f"simulation.stations.{index}.service_time.{field_name}",
                        description=f"调整工位 {station.name} 服务时间的 {field_name} 参数。",
                    )
                )
        actions.append(
            AllowedAction(
                action_type="enable_resource",
                target=station.name,
                description=(
                    "启用或停用已登记工位；整数值表示启用并设置容量，布尔值表示启用/停用。"
                ),
            )
        )
    return actions


def proposed_action_to_workflow(action: ProposedAction) -> Action:
    """把 AI 提案动作转成工作流动作模型，并校验取值形态。"""

    if action.action_type == "set_parameter":
        return SetParameterAction(
            action="set_parameter",
            path=action.target,
            value=action.proposed_value,
        )
    if action.action_type == "change_policy":
        return ChangePolicyAction(
            action="change_policy",
            policy=action.target,
            value=action.proposed_value,
        )

    value = action.proposed_value
    if value is None:
        return EnableResourceAction(action="enable_resource", resource=action.target)
    if isinstance(value, bool):
        return EnableResourceAction(
            action="enable_resource",
            resource=action.target,
            enabled=value,
        )
    if isinstance(value, int) and value >= 1:
        return EnableResourceAction(
            action="enable_resource",
            resource=action.target,
            enabled=True,
            capacity=value,
        )
    raise ActionPolicyError(
        "enable_resource proposed_value must be null, a boolean, or a positive integer"
    )


def apply_action_to_config(
    config: ProjectConfig,
    action: Action,
    *,
    resources: Mapping[str, StationConfig] | None = None,
    limits: PolicyLimits | None = None,
) -> ProjectConfig:
    """Apply one already-approved action to a copy, then revalidate the full config."""

    resource_definitions = dict(resources or resource_catalog(config))
    allowed_pairs = {(item.action_type, item.target) for item in allowed_actions(config)}

    if isinstance(action, SetParameterAction):
        pair = ("set_parameter", action.path)
        if pair not in allowed_pairs:
            raise ActionPolicyError(f"parameter target is not allowlisted: {action.path}")
        data = config.model_dump(mode="python")
        _set_dotted_value(data, action.path, deepcopy(action.value))
    elif isinstance(action, ChangePolicyAction):
        pair = ("change_policy", action.policy)
        if pair not in allowed_pairs:
            raise ActionPolicyError(f"policy target is not allowlisted: {action.policy}")
        if not isinstance(action.value, bool):
            raise ActionPolicyError(f"policy {action.policy} requires a boolean value")
        data = config.model_dump(mode="python")
        _set_dotted_value(data, action.policy, action.value)
    elif isinstance(action, EnableResourceAction):
        pair = ("enable_resource", action.resource)
        if pair not in allowed_pairs and action.resource not in resource_definitions:
            raise ActionPolicyError(f"resource is not registered: {action.resource}")
        data = config.model_dump(mode="python")
        stations = list(data["simulation"]["stations"])
        # 保留变更前的工位列表，用于重映射参数网格中按下标引用的路径。
        previous_stations = deepcopy(stations)
        current_index = next(
            (index for index, item in enumerate(stations) if item["name"] == action.resource),
            None,
        )
        if action.enabled:
            if current_index is None:
                definition = resource_definitions.get(action.resource)
                if definition is None:
                    raise ActionPolicyError(f"resource is not registered: {action.resource}")
                by_name = {item["name"]: item for item in stations}
                by_name[action.resource] = definition.model_dump(mode="python")
                stations = [by_name[name] for name in resource_definitions if name in by_name]
                current_index = next(
                    index for index, item in enumerate(stations) if item["name"] == action.resource
                )
            if action.capacity is not None:
                stations[current_index]["capacity"] = action.capacity
        elif current_index is not None:
            stations.pop(current_index)
        data["simulation"]["stations"] = stations
        _rebase_station_parameter_grid(data, previous_stations, stations)
    else:  # pragma: no cover - the discriminated union makes this unreachable.
        raise ActionPolicyError("unsupported action type")

    try:
        updated = ProjectConfig.model_validate(data, strict=True)
    except (TypeError, ValueError) as error:
        raise ActionPolicyError(
            f"action would create an invalid project config: {error}"
        ) from error
    validate_workload(updated, limits or PolicyLimits())
    return updated


def validate_workload(config: ProjectConfig, limits: PolicyLimits | None = None) -> None:
    """估算配置的服务器工作量（到达数、场景数、事件总量），超限则拒绝。"""

    limits = limits or PolicyLimits()
    _assert_finite(config.model_dump(mode="python"))
    _estimated_arrivals(config.simulation, limits, context="base simulation")
    if config.experiment.replications > limits.max_replications:
        raise WorkloadLimitError(f"replications exceed server limit {limits.max_replications}")
    scenario_count = math.prod(len(values) for values in config.experiment.parameter_grid.values())
    if not config.experiment.parameter_grid:
        scenario_count = 1
    if scenario_count > limits.max_scenarios:
        raise WorkloadLimitError(f"scenario count exceeds server limit {limits.max_scenarios}")
    total = scenario_count * config.experiment.replications
    if total > limits.max_total_replications:
        raise WorkloadLimitError(
            f"total replications {total} exceed server limit {limits.max_total_replications}"
        )
    from simlab.experiment import expand_scenarios

    try:
        scenarios = expand_scenarios(config)
    except (TypeError, ValueError) as error:
        raise ActionPolicyError(f"parameter_grid is invalid: {error}") from error

    estimated_events = 0
    for scenario_name, _parameters, simulation in scenarios:
        _assert_finite(simulation.model_dump(mode="python"))
        arrivals = _estimated_arrivals(
            simulation,
            limits,
            context=f"scenario {scenario_name}",
        )
        estimated_events += (
            arrivals * (len(simulation.stations) + 1) * config.experiment.replications
        )
    if estimated_events > limits.max_estimated_events:
        raise WorkloadLimitError(
            "estimated event workload "
            f"{estimated_events} exceeds server limit {limits.max_estimated_events}"
        )


def _estimated_arrivals(
    simulation: SimulationConfig,
    limits: PolicyLimits,
    *,
    context: str,
) -> int:
    if simulation.until > limits.max_until:
        raise WorkloadLimitError(f"{context} until exceeds server limit {limits.max_until:g}")
    if len(simulation.stations) > limits.max_stations:
        raise WorkloadLimitError(
            f"{context} station count exceeds server limit {limits.max_stations}"
        )

    if simulation.max_arrivals is not None:
        arrivals = simulation.max_arrivals
    else:
        # 未显式限制到达数时，按到达间隔分布均值估算单轮到达量上限。
        distribution = simulation.arrival_interarrival
        if distribution.kind == "exponential":
            mean_interarrival = float(distribution.mean)
        elif distribution.kind == "deterministic":
            mean_interarrival = float(distribution.value)
        elif distribution.kind == "uniform":
            mean_interarrival = (float(distribution.low) + float(distribution.high)) / 2
        else:
            mean_interarrival = (
                float(distribution.low) + float(distribution.mode) + float(distribution.high)
            ) / 3
        raw_estimate = simulation.until / mean_interarrival + 1
        if not math.isfinite(raw_estimate):
            raise WorkloadLimitError(f"{context} estimated arrivals are not finite")
        arrivals = math.ceil(raw_estimate)

    if arrivals > limits.max_arrivals_per_replication:
        raise WorkloadLimitError(
            f"{context} estimated arrivals {arrivals} exceed per-replication limit "
            f"{limits.max_arrivals_per_replication}"
        )
    return arrivals


def _set_dotted_value(data: dict[str, Any], path: str, value: Any) -> None:
    # 与 experiment.set_dotted_value 同构，但错误统一转成 ActionPolicyError。
    parts = path.split(".")
    cursor: Any = data
    try:
        for part in parts[:-1]:
            cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
        leaf = parts[-1]
        if isinstance(cursor, list):
            cursor[int(leaf)] = value
        else:
            if leaf not in cursor:
                raise KeyError(leaf)
            cursor[leaf] = value
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise ActionPolicyError(f"target path does not exist: {path}") from error


def _rebase_station_parameter_grid(
    data: dict[str, Any],
    previous_stations: list[dict[str, Any]],
    current_stations: list[dict[str, Any]],
) -> None:
    # 停用/启用会改变 stations 下标；把参数网格中的下标路径按工位名称重映射。
    grid = data["experiment"].get("parameter_grid", {})
    if not grid:
        return
    current_index = {station["name"]: index for index, station in enumerate(current_stations)}
    rebased: dict[str, list[Any]] = {}
    pattern = re.compile(r"^(simulation\.)?stations\.(\d+)(\..+)$")
    for path, values in grid.items():
        match = pattern.fullmatch(path)
        if match is None:
            next_path = path
        else:
            old_index = int(match.group(2))
            if old_index >= len(previous_stations):
                raise ActionPolicyError(f"parameter_grid station index is invalid: {path}")
            station_name = previous_stations[old_index]["name"]
            if station_name not in current_index:
                raise ActionPolicyError(
                    f"cannot disable a resource referenced by parameter_grid: {station_name}"
                )
            prefix = match.group(1) or ""
            next_path = f"{prefix}stations.{current_index[station_name]}{match.group(3)}"
        if next_path in rebased:
            raise ActionPolicyError(f"parameter_grid path collision after rebasing: {next_path}")
        rebased[next_path] = values
    data["experiment"]["parameter_grid"] = rebased


def _assert_finite(value: Any) -> None:
    # 拒绝 NaN/Inf：它们会破坏 JSON 序列化与后续统计计算。
    if isinstance(value, float) and not math.isfinite(value):
        raise WorkloadLimitError("non-finite numbers are not allowed")
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_finite(item)


__all__ = [
    "ActionPolicyError",
    "PolicyLimits",
    "WorkloadLimitError",
    "allowed_actions",
    "apply_action_to_config",
    "canonical_config_hash",
    "proposed_action_to_workflow",
    "resource_catalog",
    "validate_workload",
]
