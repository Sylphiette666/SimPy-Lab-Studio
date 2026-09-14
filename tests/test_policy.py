from __future__ import annotations

import pytest

from simlab.agent import ProposedAction
from simlab.config import ProjectConfig
from simlab.experiment import expand_scenarios
from simlab.policy import (
    ActionPolicyError,
    PolicyLimits,
    WorkloadLimitError,
    allowed_actions,
    apply_action_to_config,
    canonical_config_hash,
    proposed_action_to_workflow,
    resource_catalog,
    validate_workload,
)
from simlab.workflow import ChangePolicyAction, EnableResourceAction, SetParameterAction


def project_config() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project_name": "safe-control-test",
            "simulation": {
                "until": 100,
                "warmup": 10,
                "arrival_interarrival": {"kind": "exponential", "mean": 3.0},
                "stations": [
                    {
                        "name": "desk",
                        "capacity": 1,
                        "service_time": {"kind": "deterministic", "value": 2.0},
                    },
                    {
                        "name": "review",
                        "capacity": 2,
                        "service_time": {"kind": "exponential", "mean": 4.0},
                    },
                ],
            },
            "experiment": {"replications": 4, "parameter_grid": {}},
        }
    )


def test_allowlist_exposes_exact_safe_targets_only() -> None:
    pairs = {(item.action_type, item.target) for item in allowed_actions(project_config())}

    assert ("set_parameter", "simulation.stations.0.capacity") in pairs
    assert ("change_policy", "experiment.common_random_numbers") in pairs
    assert ("enable_resource", "desk") in pairs
    assert all("output_dir" not in target for _, target in pairs)
    assert all(not target.startswith("openai.") for _, target in pairs)
    assert all("base_seed" not in target for _, target in pairs)


def test_proposed_action_is_converted_to_inert_workflow_action() -> None:
    proposed = ProposedAction(
        action_type="set_parameter",
        target="simulation.stations.0.capacity",
        proposed_value=3,
        rationale="队列较长",
        expected_effect="降低等待",
        risk="low",
        requires_approval=True,
    )

    action = proposed_action_to_workflow(proposed)

    assert isinstance(action, SetParameterAction)
    assert action.path == "simulation.stations.0.capacity"
    assert action.value == 3


def test_approved_actions_update_a_copy_and_keep_source_immutable() -> None:
    original = project_config()
    updated = apply_action_to_config(
        original,
        SetParameterAction(
            action="set_parameter",
            path="simulation.stations.0.capacity",
            value=3,
        ),
    )
    policy_updated = apply_action_to_config(
        updated,
        ChangePolicyAction(
            action="change_policy",
            policy="experiment.common_random_numbers",
            value=False,
        ),
    )

    assert original.simulation.stations[0].capacity == 1
    assert updated.simulation.stations[0].capacity == 3
    assert policy_updated.experiment.common_random_numbers is False
    assert canonical_config_hash(original) != canonical_config_hash(updated)


def test_registered_resource_can_be_disabled_and_restored() -> None:
    original = project_config()
    catalog = resource_catalog(original)
    disabled = apply_action_to_config(
        original,
        EnableResourceAction(action="enable_resource", resource="review", enabled=False),
        resources=catalog,
    )
    restored = apply_action_to_config(
        disabled,
        EnableResourceAction(action="enable_resource", resource="review", enabled=True, capacity=4),
        resources=catalog,
    )

    assert [station.name for station in disabled.simulation.stations] == ["desk"]
    assert [station.name for station in restored.simulation.stations] == ["desk", "review"]
    assert restored.simulation.stations[-1].capacity == 4


def test_restored_resource_keeps_original_serial_route_order() -> None:
    original = project_config()
    catalog = resource_catalog(original)
    disabled = apply_action_to_config(
        original,
        EnableResourceAction(action="enable_resource", resource="desk", enabled=False),
        resources=catalog,
    )
    restored = apply_action_to_config(
        disabled,
        EnableResourceAction(action="enable_resource", resource="desk", enabled=True),
        resources=catalog,
    )

    assert [station.name for station in restored.simulation.stations] == ["desk", "review"]


def test_parameter_grid_is_rebased_by_station_name_after_disable() -> None:
    data = project_config().model_dump(mode="python")
    data["simulation"]["stations"].append(
        {
            "name": "pack",
            "capacity": 1,
            "service_time": {"kind": "deterministic", "value": 1.0},
        }
    )
    data["experiment"]["parameter_grid"] = {"stations.2.capacity": [7]}
    original = ProjectConfig.model_validate(data)

    updated = apply_action_to_config(
        original,
        EnableResourceAction(action="enable_resource", resource="review", enabled=False),
        resources=resource_catalog(original),
    )

    assert updated.experiment.parameter_grid == {"stations.1.capacity": [7]}
    scenarios = expand_scenarios(updated)
    assert scenarios[0][2].stations[1].name == "pack"
    assert scenarios[0][2].stations[1].capacity == 7


def test_resource_referenced_by_parameter_grid_cannot_be_disabled() -> None:
    data = project_config().model_dump(mode="python")
    data["experiment"]["parameter_grid"] = {"stations.1.capacity": [3]}
    original = ProjectConfig.model_validate(data)

    with pytest.raises(ActionPolicyError, match="referenced by parameter_grid"):
        apply_action_to_config(
            original,
            EnableResourceAction(action="enable_resource", resource="review", enabled=False),
            resources=resource_catalog(original),
        )


def test_unknown_or_invalid_action_is_blocked_before_execution() -> None:
    config = project_config()
    with pytest.raises(ActionPolicyError, match="not allowlisted"):
        apply_action_to_config(
            config,
            SetParameterAction(
                action="set_parameter",
                path="openai.store",
                value=True,
            ),
        )
    with pytest.raises(ActionPolicyError, match="invalid project config"):
        apply_action_to_config(
            config,
            SetParameterAction(
                action="set_parameter",
                path="simulation.warmup",
                value=100,
            ),
        )


def test_action_values_are_not_coerced_to_target_types() -> None:
    with pytest.raises(ActionPolicyError, match="invalid project config"):
        apply_action_to_config(
            project_config(),
            SetParameterAction(
                action="set_parameter",
                path="simulation.stations.0.capacity",
                value="3",
            ),
        )


def test_workload_limits_bound_api_cost() -> None:
    config = project_config().model_copy(deep=True)
    config.experiment.replications = 20

    with pytest.raises(WorkloadLimitError, match="replications"):
        validate_workload(config, PolicyLimits(max_replications=10))


def test_parameter_grid_cannot_bypass_scenario_runtime_limit() -> None:
    data = project_config().model_dump(mode="python")
    data["simulation"]["until"] = 10
    data["simulation"]["warmup"] = 0
    data["experiment"]["parameter_grid"] = {"until": [1_000_000_000.0]}
    config = ProjectConfig.model_validate(data)

    with pytest.raises(WorkloadLimitError, match="scenario .* until"):
        validate_workload(config, PolicyLimits(max_until=100))


def test_arrival_budget_blocks_event_explosion_but_honors_explicit_cap() -> None:
    data = project_config().model_dump(mode="python")
    data["simulation"]["until"] = 100
    data["simulation"]["warmup"] = 0
    data["simulation"]["arrival_interarrival"] = {
        "kind": "deterministic",
        "value": 0.001,
    }
    unbounded = ProjectConfig.model_validate(data)

    with pytest.raises(WorkloadLimitError, match="estimated arrivals"):
        validate_workload(
            unbounded,
            PolicyLimits(max_arrivals_per_replication=10_000),
        )

    data["simulation"]["max_arrivals"] = 100
    bounded = ProjectConfig.model_validate(data)
    validate_workload(
        bounded,
        PolicyLimits(
            max_arrivals_per_replication=10_000,
            max_estimated_events=10_000,
        ),
    )
