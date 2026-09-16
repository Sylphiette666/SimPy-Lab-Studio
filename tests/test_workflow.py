from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from simlab.config import ExperimentConfig
from simlab.workflow import (
    ApprovalWorkflow,
    AuditEventType,
    IdempotencyConflict,
    InvalidTransition,
    ProposalStatus,
    VersionConflict,
    validate_action,
)


def approve_and_apply(
    workflow: ApprovalWorkflow,
    action: dict,
    operation_prefix: str,
):
    proposal = workflow.propose(
        action,
        operation_id=f"{operation_prefix}-propose",
        expected_version=workflow.version,
    )
    workflow.approve(
        proposal.proposal_id,
        operation_id=f"{operation_prefix}-approve",
        expected_version=workflow.version,
        actor="reviewer",
    )
    return workflow.apply(
        proposal.proposal_id,
        operation_id=f"{operation_prefix}-apply",
        expected_version=workflow.version,
    )


def test_action_whitelist_and_strict_validation() -> None:
    assert (
        validate_action(
            {"action": "set_parameter", "path": "stations.0.capacity", "value": 3}
        ).action
        == "set_parameter"
    )

    with pytest.raises(ValidationError):
        validate_action({"action": "run_python", "source": "print('unsafe')"})
    with pytest.raises(ValidationError):
        validate_action(
            {
                "action": "change_policy",
                "policy": "shortest_queue",
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        validate_action({"action": "change_policy", "policy": 7})
    with pytest.raises(ValidationError):
        validate_action({"action": "enable_resource", "resource": "desk", "enabled": 1})
    with pytest.raises(ValidationError):
        validate_action(
            {"action": "enable_resource", "resource": "desk", "enabled": False, "capacity": 2}
        )
    with pytest.raises(ValidationError):
        validate_action({"action": "set_parameter", "path": "unsafe", "value": lambda: None})


def test_happy_path_has_monotonic_versions_and_audit_log() -> None:
    workflow = ApprovalWorkflow(session_id="session-1")
    proposal = workflow.propose(
        {"action": "set_parameter", "path": "stations.0.capacity", "value": 3},
        operation_id="op-propose",
        expected_version=0,
    )
    assert proposal.status is ProposalStatus.PENDING
    assert proposal.created_version == 1
    assert workflow.version == 1

    approved = workflow.approve(
        proposal.proposal_id,
        operation_id="op-approve",
        expected_version=1,
        actor="alice",
        reason="capacity test is within scope",
    )
    assert approved.status is ProposalStatus.APPROVED
    assert approved.updated_version == 2

    applied = workflow.apply(
        proposal.proposal_id,
        operation_id="op-apply",
        expected_version=2,
    )
    assert applied.status is ProposalStatus.APPLIED
    assert applied.updated_version == 3
    assert workflow.snapshot().state.parameters["stations.0.capacity"] == 3

    audit = workflow.audit_log()
    assert [entry.sequence for entry in audit] == [1, 2, 3]
    assert [entry.version for entry in audit] == [1, 2, 3]
    assert [entry.event for entry in audit] == [
        AuditEventType.PROPOSAL_CREATED,
        AuditEventType.PROPOSAL_APPROVED,
        AuditEventType.PROPOSAL_APPLIED,
    ]


def test_change_policy_and_enable_resource_update_only_typed_state() -> None:
    workflow = ApprovalWorkflow()

    approve_and_apply(
        workflow,
        {
            "action": "change_policy",
            "policy": "shortest_queue",
            "value": {"tie_breaker": "fifo"},
        },
        "policy",
    )
    approve_and_apply(
        workflow,
        {"action": "enable_resource", "resource": "overflow-desk", "capacity": 2},
        "resource-on",
    )
    state = workflow.snapshot().state
    assert state.policy == "shortest_queue"
    assert state.policy_value == {"tie_breaker": "fifo"}
    assert state.enabled_resources == ("overflow-desk",)
    assert state.resource_capacities == {"overflow-desk": 2}

    approve_and_apply(
        workflow,
        {"action": "enable_resource", "resource": "overflow-desk", "enabled": False},
        "resource-off",
    )
    state = workflow.snapshot().state
    assert state.enabled_resources == ()
    assert state.resource_capacities == {}


def test_experiment_policy_updates_typed_experiment_state() -> None:
    workflow = ApprovalWorkflow(ExperimentConfig(common_random_numbers=True))

    result = approve_and_apply(
        workflow,
        {
            "action": "change_policy",
            "policy": "experiment.common_random_numbers",
            "value": False,
        },
        "crn-off",
    )

    state = workflow.snapshot().state
    assert result.status is ProposalStatus.APPLIED
    assert state.experiment.common_random_numbers is False
    assert state.policy == "experiment.common_random_numbers"
    assert state.policy_value is False


def test_rejected_proposal_cannot_be_applied() -> None:
    workflow = ApprovalWorkflow()
    proposal = workflow.create_proposal(
        {"action": "change_policy", "policy": "fifo"},
        operation_id="reject-propose",
        expected_version=0,
    )
    rejected = workflow.reject_proposal(
        proposal.proposal_id,
        operation_id="reject-decision",
        expected_version=1,
        reason="not approved for production",
    )
    assert rejected.status is ProposalStatus.REJECTED

    with pytest.raises(InvalidTransition):
        workflow.apply_proposal(
            proposal.proposal_id,
            operation_id="reject-apply",
            expected_version=2,
        )
    assert workflow.version == 2
    assert len(workflow.audit_log()) == 2


def test_expected_version_prevents_lost_updates() -> None:
    workflow = ApprovalWorkflow()
    workflow.propose(
        {"action": "change_policy", "policy": "fifo"},
        operation_id="first",
        expected_version=0,
    )

    with pytest.raises(VersionConflict) as caught:
        workflow.propose(
            {"action": "change_policy", "policy": "priority"},
            operation_id="stale",
            expected_version=0,
        )
    assert caught.value.expected_version == 0
    assert caught.value.current_version == 1
    assert workflow.version == 1

    with pytest.raises(ValidationError):
        workflow.propose(
            {"action": "change_policy", "policy": "priority"},
            operation_id="bool-version",
            expected_version=False,
        )


def test_operation_id_is_idempotent_but_cannot_be_rebound() -> None:
    workflow = ApprovalWorkflow()
    payload = {"action": "change_policy", "policy": "fifo"}
    first = workflow.propose(payload, operation_id="same-op", expected_version=0)
    replay = workflow.propose(payload, operation_id="same-op", expected_version=0)

    assert replay == first
    assert workflow.version == 1
    assert len(workflow.audit_log()) == 1

    with pytest.raises(IdempotencyConflict):
        workflow.propose(
            {"action": "change_policy", "policy": "priority"},
            operation_id="same-op",
            expected_version=1,
        )


def test_invalid_experiment_update_becomes_failed_without_mutating_state() -> None:
    workflow = ApprovalWorkflow(ExperimentConfig(replications=5))
    proposal = workflow.propose(
        {"action": "set_parameter", "path": "experiment.replications", "value": 0},
        operation_id="bad-propose",
        expected_version=0,
    )
    workflow.approve(
        proposal.proposal_id,
        operation_id="bad-approve",
        expected_version=1,
    )
    failed = workflow.apply(
        proposal.proposal_id,
        operation_id="bad-apply",
        expected_version=2,
    )

    assert failed.status is ProposalStatus.FAILED
    assert failed.error
    assert workflow.snapshot().state.experiment.replications == 5
    assert workflow.version == 3
    assert workflow.audit_log()[-1].event is AuditEventType.PROPOSAL_FAILED


def test_valid_experiment_update_reuses_experiment_config_validation() -> None:
    workflow = ApprovalWorkflow(ExperimentConfig(replications=5))
    result = approve_and_apply(
        workflow,
        {"action": "set_parameter", "path": "experiment.replications", "value": 25},
        "experiment",
    )
    assert result.status is ProposalStatus.APPLIED
    assert workflow.snapshot().state.experiment.replications == 25


def test_model_supplied_source_text_is_stored_inert(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist.txt"
    source_text = f"__import__('pathlib').Path({str(marker)!r}).write_text('unsafe')"
    workflow = ApprovalWorkflow()

    result = approve_and_apply(
        workflow,
        {"action": "set_parameter", "path": "notes.model_output", "value": source_text},
        "inert",
    )

    assert result.status is ProposalStatus.APPLIED
    assert workflow.snapshot().state.parameters["notes.model_output"] == source_text
    assert not marker.exists()


def test_returned_snapshots_do_not_alias_internal_mutable_data() -> None:
    workflow = ApprovalWorkflow(initial_parameters={"nested.value": {"items": [1]}})
    snapshot = workflow.snapshot()
    snapshot.state.parameters["nested.value"]["items"].append(2)

    assert workflow.snapshot().state.parameters["nested.value"] == {"items": [1]}
