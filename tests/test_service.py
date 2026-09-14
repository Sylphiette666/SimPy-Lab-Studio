from __future__ import annotations

from pathlib import Path

import pytest

from simlab.agent import ActionProposal, ProposedAction
from simlab.config import ProjectConfig
from simlab.policy import ActionPolicyError
from simlab.service import RunExecutionError, SimulationControlService, StaleProposal
from simlab.workflow import SetParameterAction, VersionConflict


def config() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project_name": "control-service-test",
            "simulation": {
                "until": 20,
                "warmup": 0,
                "max_arrivals": 20,
                "arrival_interarrival": {"kind": "deterministic", "value": 2},
                "stations": [
                    {
                        "name": "desk",
                        "capacity": 1,
                        "service_time": {"kind": "deterministic", "value": 1},
                    }
                ],
            },
            "experiment": {"replications": 2, "base_seed": 42},
        }
    )


def config_with_review_station() -> ProjectConfig:
    data = config().model_dump(mode="python")
    data["simulation"]["stations"].append(
        {
            "name": "review",
            "capacity": 1,
            "service_time": {"kind": "deterministic", "value": 2},
        }
    )
    return ProjectConfig.model_validate(data)


def test_manual_proposal_requires_approval_before_config_changes(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    created = service.create_session(config(), session_id="session-test")
    proposal = service.submit_action(
        created.session_id,
        SetParameterAction(
            action="set_parameter",
            path="simulation.stations.0.capacity",
            value=2,
        ),
        operation_id="propose-1",
        expected_version=0,
    )

    pending = service.get_session(created.session_id)
    assert pending.config.simulation.stations[0].capacity == 1
    assert pending.workflow_version == 1

    applied = service.approve_and_apply(
        created.session_id,
        proposal.proposal_id,
        operation_id="decision-1",
        expected_version=1,
        actor="approver@example.test",
        reason="已检查容量预算",
    )

    assert applied.status == "applied"
    updated = service.get_session(created.session_id)
    assert updated.config.simulation.stations[0].capacity == 2
    assert updated.workflow_version == 3
    assert [event.sequence for event in service.list_events(created.session_id)] == [1, 2, 3]


def test_approval_operation_is_idempotent(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    proposal = service.submit_action(
        session.session_id,
        {
            "action": "set_parameter",
            "path": "experiment.replications",
            "value": 3,
        },
        operation_id="propose-idempotent",
        expected_version=0,
    )
    first = service.approve_and_apply(
        session.session_id,
        proposal.proposal_id,
        operation_id="approve-idempotent",
        expected_version=1,
        actor="alice",
    )
    second = service.approve_and_apply(
        session.session_id,
        proposal.proposal_id,
        operation_id="approve-idempotent",
        expected_version=1,
        actor="alice",
    )

    assert second == first
    assert service.get_session(session.session_id).workflow_version == 3


def test_client_operation_id_cannot_collide_with_internal_approval_phases(
    tmp_path: Path,
) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    service.submit_action(
        session.session_id,
        {
            "action": "set_parameter",
            "path": "simulation.until",
            "value": 25,
        },
        operation_id="decision:apply",
        expected_version=0,
    )
    target = service.submit_action(
        session.session_id,
        {
            "action": "set_parameter",
            "path": "experiment.replications",
            "value": 3,
        },
        operation_id="target-proposal",
        expected_version=1,
    )

    applied = service.approve_and_apply(
        session.session_id,
        target.proposal_id,
        operation_id="decision",
        expected_version=2,
        actor="alice",
    )

    assert applied.status == "applied"
    assert service.get_session(session.session_id).config.experiment.replications == 3


def test_internal_operation_prefix_is_reserved(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())

    with pytest.raises(ValueError, match="reserved"):
        service.submit_action(
            session.session_id,
            {
                "action": "set_parameter",
                "path": "experiment.replications",
                "value": 3,
            },
            operation_id="simlab:forged-internal-id",
            expected_version=0,
        )


def test_proposal_submission_is_idempotent_after_version_advances(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    request = {
        "action": "set_parameter",
        "path": "experiment.replications",
        "value": 3,
    }
    first = service.submit_action(
        session.session_id,
        request,
        operation_id="proposal-retry",
        expected_version=0,
    )
    second = service.submit_action(
        session.session_id,
        request,
        operation_id="proposal-retry",
        expected_version=0,
    )

    assert second == first
    assert service.get_session(session.session_id).workflow_version == 1


def test_proposal_becomes_stale_after_another_config_is_applied(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    first = service.submit_action(
        session.session_id,
        {
            "action": "set_parameter",
            "path": "simulation.stations.0.capacity",
            "value": 2,
        },
        operation_id="p-first",
        expected_version=0,
    )
    second = service.submit_action(
        session.session_id,
        {
            "action": "set_parameter",
            "path": "simulation.until",
            "value": 30,
        },
        operation_id="p-second",
        expected_version=1,
    )
    service.approve_and_apply(
        session.session_id,
        first.proposal_id,
        operation_id="approve-first",
        expected_version=2,
        actor="alice",
    )

    with pytest.raises(StaleProposal):
        service.approve_and_apply(
            session.session_id,
            second.proposal_id,
            operation_id="approve-second",
            expected_version=4,
            actor="alice",
        )


class StubAgent:
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, current_config, kpi_summary, allowed_actions, objective=None):
        self.calls += 1
        assert current_config["project_name"] == "control-service-test"
        assert any(item.target == "experiment.replications" for item in allowed_actions)
        return ActionProposal(
            summary="建议增加重复次数",
            actions=[
                ProposedAction(
                    action_type="set_parameter",
                    target="experiment.replications",
                    proposed_value=3,
                    rationale="降低标准误",
                    expected_effect="提高估计稳定性",
                    risk="low",
                    requires_approval=True,
                )
            ],
            caveats=["需要额外计算时间"],
        )


class MultiActionAgent(StubAgent):
    def propose(self, current_config, kpi_summary, allowed_actions, objective=None):
        self.calls += 1
        return ActionProposal(
            summary="按顺序扩大实验",
            actions=[
                ProposedAction(
                    action_type="set_parameter",
                    target="experiment.replications",
                    proposed_value=3,
                    rationale="提高稳定性",
                    expected_effect="降低标准误",
                    risk="low",
                    requires_approval=True,
                ),
                ProposedAction(
                    action_type="set_parameter",
                    target="simulation.until",
                    proposed_value=30,
                    rationale="覆盖更长时间",
                    expected_effect="增加观察量",
                    risk="low",
                    requires_approval=True,
                ),
            ],
            caveats=[],
        )


class UnsafeTopologyPlanAgent(StubAgent):
    def propose(self, current_config, kpi_summary, allowed_actions, objective=None):
        self.calls += 1
        return ActionProposal(
            summary="不安全的下标顺序",
            actions=[
                ProposedAction(
                    action_type="enable_resource",
                    target="desk",
                    proposed_value=False,
                    rationale="停用首个工位",
                    expected_effect="改变拓扑",
                    risk="high",
                    requires_approval=True,
                ),
                ProposedAction(
                    action_type="set_parameter",
                    target="simulation.stations.1.capacity",
                    proposed_value=9,
                    rationale="修改原第二工位",
                    expected_effect="增加容量",
                    risk="high",
                    requires_approval=True,
                ),
            ],
            caveats=[],
        )


def test_ai_plan_only_creates_pending_proposals(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    agent = StubAgent()

    plan = service.submit_ai_plan(
        session.session_id,
        agent,  # type: ignore[arg-type]
        operation_id="ai-plan-1",
        expected_version=0,
        objective="提高置信度",
        kpi_summary=[],
    )
    replay = service.submit_ai_plan(
        session.session_id,
        agent,  # type: ignore[arg-type]
        operation_id="ai-plan-1",
        expected_version=0,
        objective="提高置信度",
        kpi_summary=[],
    )

    assert plan.proposals[0].status == "pending"
    assert replay == plan
    assert agent.calls == 1
    assert service.get_session(session.session_id).config.experiment.replications == 2


def test_multi_action_ai_plan_can_be_applied_in_order(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    plan = service.submit_ai_plan(
        session.session_id,
        MultiActionAgent(),  # type: ignore[arg-type]
        operation_id="ai-sequence",
        expected_version=0,
        kpi_summary=[],
    )

    first, second = plan.proposals
    service.approve_and_apply(
        session.session_id,
        first.proposal_id,
        operation_id="approve-ai-step-1",
        expected_version=2,
        actor="alice",
    )
    service.approve_and_apply(
        session.session_id,
        second.proposal_id,
        operation_id="approve-ai-step-2",
        expected_version=4,
        actor="alice",
    )

    updated = service.get_session(session.session_id).config
    assert updated.experiment.replications == 3
    assert updated.simulation.until == 30


def test_ai_plan_idempotency_survives_later_config_change(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    agent = StubAgent()
    plan = service.submit_ai_plan(
        session.session_id,
        agent,  # type: ignore[arg-type]
        operation_id="ai-replay-after-change",
        expected_version=0,
        kpi_summary=[],
    )
    service.approve_and_apply(
        session.session_id,
        plan.proposals[0].proposal_id,
        operation_id="apply-ai-change",
        expected_version=1,
        actor="alice",
    )

    replay = service.submit_ai_plan(
        session.session_id,
        agent,  # type: ignore[arg-type]
        operation_id="ai-replay-after-change",
        expected_version=0,
        kpi_summary=[],
    )

    assert replay == plan
    assert agent.calls == 1


def test_ai_plan_rejects_oversized_operation_id_before_partial_creation(
    tmp_path: Path,
) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    agent = MultiActionAgent()

    with pytest.raises(ValueError, match="operation_id"):
        service.submit_ai_plan(
            session.session_id,
            agent,  # type: ignore[arg-type]
            operation_id="a" * 91,
            expected_version=0,
            kpi_summary=[],
        )

    current = service.get_session(session.session_id)
    assert agent.calls == 0
    assert current.workflow_version == 0
    assert current.workflow.proposals == ()


def test_ai_plan_rejects_station_index_after_topology_change(tmp_path: Path) -> None:
    data = config_with_review_station().model_dump(mode="python")
    data["simulation"]["stations"].append(
        {
            "name": "pack",
            "capacity": 1,
            "service_time": {"kind": "deterministic", "value": 1},
        }
    )
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(ProjectConfig.model_validate(data))

    with pytest.raises(ActionPolicyError, match="indexed station parameter"):
        service.submit_ai_plan(
            session.session_id,
            UnsafeTopologyPlanAgent(),  # type: ignore[arg-type]
            operation_id="unsafe-topology-plan",
            expected_version=0,
            kpi_summary=[],
        )

    current = service.get_session(session.session_id)
    assert current.workflow_version == 0
    assert current.workflow.proposals == ()


def test_run_uses_immutable_snapshot_and_server_assigned_directory(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())

    record = service.run(
        session.session_id,
        operation_id="run-1",
        expected_version=0,
        workers=1,
    )
    replay = service.run(
        session.session_id,
        operation_id="run-1",
        expected_version=0,
        workers=1,
    )

    assert record.status == "succeeded"
    assert record.result is not None
    assert record.result["schema_version"] == "1.1"
    assert Path(record.output_dir).is_relative_to(tmp_path.resolve())
    assert (Path(record.output_dir) / "results.json").exists()
    assert replay.run_id == record.run_id


def test_run_idempotency_survives_later_config_change(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    original_run = service.run(
        session.session_id,
        operation_id="run-before-change",
        expected_version=0,
    )
    proposal = service.submit_action(
        session.session_id,
        {
            "action": "set_parameter",
            "path": "simulation.until",
            "value": 30,
        },
        operation_id="change-after-run",
        expected_version=0,
    )
    service.approve_and_apply(
        session.session_id,
        proposal.proposal_id,
        operation_id="approve-after-run",
        expected_version=1,
        actor="alice",
    )

    replay = service.run(
        session.session_id,
        operation_id="run-before-change",
        expected_version=0,
    )

    assert replay == original_run


def test_failed_run_replay_preserves_failure_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    calls = 0

    def fail_run(self, workers=1):
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    monkeypatch.setattr("simlab.service.ExperimentRunner.run", fail_run)

    with pytest.raises(RunExecutionError, match="simulation run failed: boom"):
        service.run(
            session.session_id,
            operation_id="failed-run",
            expected_version=0,
        )
    with pytest.raises(RunExecutionError, match="simulation run failed: boom"):
        service.run(
            session.session_id,
            operation_id="failed-run",
            expected_version=0,
        )

    assert calls == 1


def test_expected_version_prevents_lost_updates(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config())
    service.submit_action(
        session.session_id,
        {
            "action": "set_parameter",
            "path": "experiment.replications",
            "value": 3,
        },
        operation_id="first",
        expected_version=0,
    )

    with pytest.raises(VersionConflict):
        service.submit_action(
            session.session_id,
            {
                "action": "set_parameter",
                "path": "experiment.replications",
                "value": 4,
            },
            operation_id="stale",
            expected_version=0,
        )


def test_session_id_cannot_escape_server_output_root(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)

    with pytest.raises(ValueError, match="session_id"):
        service.create_session(config(), session_id="safe/../../escape")


def test_resource_restore_uses_latest_approved_definition(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config_with_review_station())

    update = service.submit_action(
        session.session_id,
        {
            "action": "set_parameter",
            "path": "simulation.stations.1.service_time.value",
            "value": 6,
        },
        operation_id="update-review-time",
        expected_version=0,
    )
    service.approve_and_apply(
        session.session_id,
        update.proposal_id,
        operation_id="approve-review-time",
        expected_version=1,
        actor="alice",
    )

    disable = service.submit_action(
        session.session_id,
        {"action": "enable_resource", "resource": "review", "enabled": False},
        operation_id="disable-review",
        expected_version=3,
    )
    service.approve_and_apply(
        session.session_id,
        disable.proposal_id,
        operation_id="approve-disable-review",
        expected_version=4,
        actor="alice",
    )

    enable = service.submit_action(
        session.session_id,
        {"action": "enable_resource", "resource": "review", "enabled": True},
        operation_id="enable-review",
        expected_version=6,
    )
    service.approve_and_apply(
        session.session_id,
        enable.proposal_id,
        operation_id="approve-enable-review",
        expected_version=7,
        actor="alice",
    )

    restored = service.get_session(session.session_id).config.simulation.stations[1]
    assert restored.name == "review"
    assert restored.service_time.value == 6
