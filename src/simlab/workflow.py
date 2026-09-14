"""线程安全的审批状态机：提案从 pending 经人工审批后应用为类型化内存状态。

动作只是声明式数据；应用动作只更新类型化的内存状态，模型生成的源码
永远不会被执行。所有写操作幂等并带版本校验，冲突时抛 VersionConflict。
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from simlab.config import ExperimentConfig


class WorkflowError(RuntimeError):
    """Base class for workflow state and concurrency errors."""


class VersionConflict(WorkflowError):
    """乐观锁冲突：请求携带的版本与当前版本不一致。"""

    def __init__(self, expected_version: int, current_version: int):
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"version conflict: expected {expected_version}, current {current_version}"
        )


class IdempotencyConflict(WorkflowError):
    """Raised when an operation id is reused for a different request."""


class ProposalNotFound(WorkflowError):
    """Raised when a proposal id is unknown to this workflow session."""


class InvalidTransition(WorkflowError):
    """Raised when a proposal cannot move from its current status."""


class ProposalStatus(StrEnum):
    """提案生命周期：待审批 → 同意/拒绝 → 已应用/失败。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"


class AuditEventType(StrEnum):
    """审计事件类型，与提案状态迁移一一对应。"""

    PROPOSAL_CREATED = "proposal_created"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    PROPOSAL_APPLIED = "proposal_applied"
    PROPOSAL_FAILED = "proposal_failed"


Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$",
    ),
]
ActorName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
ReasonText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class _StrictModel(BaseModel):
    # 所有工作流模型：拒绝未知字段、严格类型、禁止 NaN/Inf。
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )


def _validate_parameter_path(path: str) -> str:
    # 语法级路径校验；白名单语义在 policy.allowed_actions 落地时再查。
    if path != path.strip() or len(path) > 256:
        raise ValueError("parameter path must be trimmed and at most 256 characters")
    parts = path.split(".")
    if not parts or any(not part for part in parts):
        raise ValueError("parameter path must contain non-empty dotted segments")
    for part in parts:
        if part.isdecimal():
            continue
        if part.startswith("__"):
            raise ValueError("dunder path segments are not allowed")
        if not (part[0].isalpha() or part[0] == "_"):
            raise ValueError("parameter path segments must start with a letter or underscore")
        if not all(character.isalnum() or character in {"_", "-"} for character in part):
            raise ValueError("parameter path contains unsupported characters")
    if path.startswith("experiment."):
        field_name = path.removeprefix("experiment.")
        if "." in field_name or field_name not in ExperimentConfig.model_fields:
            raise ValueError("experiment paths must name one ExperimentConfig field")
    elif path == "experiment":
        raise ValueError("set an explicit experiment field")
    return path


def _assert_finite_json(value: JsonValue) -> JsonValue:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not allowed")
    if isinstance(value, list):
        for item in value:
            _assert_finite_json(item)
    elif isinstance(value, dict):
        for item in value.values():
            _assert_finite_json(item)
    return value


class SetParameterAction(_StrictModel):
    """修改参数的动作；path 语法受限，落地前还需通过 policy 白名单。"""

    action: Literal["set_parameter"]
    path: str
    value: JsonValue

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_parameter_path(value)

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: JsonValue) -> JsonValue:
        return _assert_finite_json(value)


class ChangePolicyAction(_StrictModel):
    """切换布尔策略开关（如共同随机数）。"""

    action: Literal["change_policy"]
    policy: ActorName
    value: JsonValue = None

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: JsonValue) -> JsonValue:
        return _assert_finite_json(value)


class EnableResourceAction(_StrictModel):
    """启用/停用已登记资源，可选设置容量。"""

    action: Literal["enable_resource"]
    resource: ActorName
    enabled: bool = True
    capacity: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_capacity(self) -> EnableResourceAction:
        if not self.enabled and self.capacity is not None:
            raise ValueError("capacity cannot be supplied when enabled is false")
        return self


Action = Annotated[
    SetParameterAction | ChangePolicyAction | EnableResourceAction,
    Field(discriminator="action"),
]
_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def validate_action(action: Action | Mapping[str, Any]) -> Action:
    """Validate a model-supplied action as inert, JSON-compatible data."""

    return _ACTION_ADAPTER.validate_python(action, strict=True)


class WorkflowState(_StrictModel):
    """会话的类型化内存状态：参数、策略与启用的资源。"""

    experiment: ExperimentConfig = Field(default_factory=ExperimentConfig)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    policy: str | None = None
    policy_value: JsonValue = None
    enabled_resources: tuple[str, ...] = ()
    resource_capacities: dict[str, int] = Field(default_factory=dict)


class Proposal(_StrictModel):
    """一条提案记录：动作、状态、版本与审批信息。"""

    proposal_id: Identifier
    operation_id: Identifier
    action: Action
    status: ProposalStatus
    created_by: ActorName
    created_at: datetime
    created_version: int = Field(ge=1)
    updated_at: datetime
    updated_version: int = Field(ge=1)
    decided_by: str | None = None
    decision_reason: str | None = None
    error: str | None = None


class AuditEntry(_StrictModel):
    """一条不可变审计记录，sequence 单调递增。"""

    sequence: int = Field(ge=1)
    version: int = Field(ge=1)
    timestamp: datetime
    operation_id: Identifier
    event: AuditEventType
    proposal_id: Identifier
    actor: ActorName
    details: dict[str, JsonValue] = Field(default_factory=dict)


class WorkflowSession(_StrictModel):
    """会话快照：版本、状态与全部提案。"""

    session_id: Identifier
    version: int = Field(ge=0)
    state: WorkflowState
    proposals: tuple[Proposal, ...]


class _OperationContext(_StrictModel):
    operation_id: Identifier
    expected_version: int = Field(ge=0)
    actor: ActorName


class _DecisionContext(_OperationContext):
    proposal_id: Identifier
    reason: ReasonText | None = None


class _OperationRecord:
    __slots__ = ("fingerprint", "result")

    def __init__(self, fingerprint: str, result: Proposal):
        self.fingerprint = fingerprint
        self.result = result.model_copy(deep=True)


class ApprovalWorkflow:
    """Thread-safe in-memory approval state machine for a single session.

    Actions are declarative data. Applying an approved action only updates the
    typed in-memory state below; model-produced source code is never run.
    """

    def __init__(
        self,
        experiment: ExperimentConfig | Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        initial_parameters: Mapping[str, JsonValue] | None = None,
        initial_policy: str | None = None,
        initial_resources: Iterable[str] = (),
    ):
        if experiment is None:
            experiment_config = ExperimentConfig()
        elif isinstance(experiment, ExperimentConfig):
            experiment_config = experiment.model_copy(deep=True)
        else:
            experiment_config = ExperimentConfig.model_validate(dict(experiment), strict=True)

        parameters: dict[str, JsonValue] = {}
        for path, value in (initial_parameters or {}).items():
            action = SetParameterAction(action="set_parameter", path=path, value=value)
            if action.path.startswith("experiment."):
                raise ValueError("initial experiment values belong in the experiment argument")
            parameters[action.path] = deepcopy(action.value)

        policy = None
        if initial_policy is not None:
            policy = ChangePolicyAction(action="change_policy", policy=initial_policy).policy

        if isinstance(initial_resources, (str, bytes)):
            raise TypeError("initial_resources must be an iterable of resource names")
        resources = {
            EnableResourceAction(action="enable_resource", resource=resource).resource
            for resource in initial_resources
        }
        session_value = session_id or f"session-{uuid4().hex}"
        session_value = TypeAdapter(Identifier).validate_python(session_value, strict=True)

        self._session_id = session_value
        self._version = 0
        self._state = WorkflowState(
            experiment=experiment_config,
            parameters=parameters,
            policy=policy,
            policy_value=None,
            enabled_resources=tuple(sorted(resources)),
            resource_capacities={},
        )
        self._proposals: dict[str, Proposal] = {}
        self._operations: dict[str, _OperationRecord] = {}
        self._audit: list[AuditEntry] = []
        self._lock = RLock()

    @property
    def version(self) -> int:
        """当前工作流版本；每次状态变更递增 1。"""

        with self._lock:
            return self._version

    def snapshot(self) -> WorkflowSession:
        """返回会话只读快照（深拷贝）。"""

        with self._lock:
            return WorkflowSession(
                session_id=self._session_id,
                version=self._version,
                state=self._state.model_copy(deep=True),
                proposals=tuple(
                    proposal.model_copy(deep=True) for proposal in self._proposals.values()
                ),
            )

    get_session = snapshot

    def get_proposal(self, proposal_id: str) -> Proposal:
        """按 ID 返回提案副本；不存在时抛 ProposalNotFound。"""

        proposal_key = TypeAdapter(Identifier).validate_python(proposal_id, strict=True)
        with self._lock:
            proposal = self._proposals.get(proposal_key)
            if proposal is None:
                raise ProposalNotFound(f"proposal not found: {proposal_key}")
            return proposal.model_copy(deep=True)

    def list_proposals(self, status: ProposalStatus | str | None = None) -> tuple[Proposal, ...]:
        """按状态过滤返回提案副本。"""

        status_value = ProposalStatus(status) if status is not None else None
        with self._lock:
            return tuple(
                proposal.model_copy(deep=True)
                for proposal in self._proposals.values()
                if status_value is None or proposal.status == status_value
            )

    def audit_log(self) -> tuple[AuditEntry, ...]:
        """返回全部审计记录副本。"""

        with self._lock:
            return tuple(entry.model_copy(deep=True) for entry in self._audit)

    get_audit_log = audit_log

    def propose(
        self,
        action: Action | Mapping[str, Any],
        *,
        operation_id: str,
        expected_version: int,
        actor: str = "model",
    ) -> Proposal:
        """创建待审批提案（幂等），并推进工作流版本。"""

        action_value = validate_action(action).model_copy(deep=True)
        context = _OperationContext(
            operation_id=operation_id,
            expected_version=expected_version,
            actor=actor,
        )
        fingerprint = self._fingerprint(
            "propose",
            {"action": action_value.model_dump(mode="json"), "actor": context.actor},
        )
        with self._lock:
            replay = self._replay(context.operation_id, fingerprint)
            if replay is not None:
                return replay
            self._require_version(context.expected_version)

            version = self._version + 1
            now = datetime.now(UTC)
            proposal = Proposal(
                proposal_id=f"proposal-{uuid4().hex}",
                operation_id=context.operation_id,
                action=action_value,
                status=ProposalStatus.PENDING,
                created_by=context.actor,
                created_at=now,
                created_version=version,
                updated_at=now,
                updated_version=version,
            )
            self._proposals[proposal.proposal_id] = proposal
            self._version = version
            self._append_audit(
                operation_id=context.operation_id,
                event=AuditEventType.PROPOSAL_CREATED,
                proposal=proposal,
                actor=context.actor,
                details={
                    "status": ProposalStatus.PENDING.value,
                    "action": action_value.model_dump(mode="json"),
                },
            )
            self._remember(context.operation_id, fingerprint, proposal)
            return proposal.model_copy(deep=True)

    create_proposal = propose

    def approve(
        self,
        proposal_id: str,
        *,
        operation_id: str,
        expected_version: int,
        actor: str = "human",
        reason: str | None = None,
    ) -> Proposal:
        """审批通过提案；只有 pending 状态可被审批。"""

        return self._decide(
            proposal_id,
            operation_id=operation_id,
            expected_version=expected_version,
            actor=actor,
            reason=reason,
            target=ProposalStatus.APPROVED,
        )

    approve_proposal = approve

    def reject(
        self,
        proposal_id: str,
        *,
        operation_id: str,
        expected_version: int,
        actor: str = "human",
        reason: str | None = None,
    ) -> Proposal:
        """拒绝提案；只有 pending 状态可被拒绝。"""

        return self._decide(
            proposal_id,
            operation_id=operation_id,
            expected_version=expected_version,
            actor=actor,
            reason=reason,
            target=ProposalStatus.REJECTED,
        )

    reject_proposal = reject

    def apply(
        self,
        proposal_id: str,
        *,
        operation_id: str,
        expected_version: int,
        actor: str = "system",
    ) -> Proposal:
        """应用已审批的提案：更新类型化状态；校验失败则标记 FAILED。"""

        context = _DecisionContext(
            proposal_id=proposal_id,
            operation_id=operation_id,
            expected_version=expected_version,
            actor=actor,
        )
        fingerprint = self._fingerprint(
            "apply",
            {"proposal_id": context.proposal_id, "actor": context.actor},
        )
        with self._lock:
            replay = self._replay(context.operation_id, fingerprint)
            if replay is not None:
                return replay
            self._require_version(context.expected_version)
            proposal = self._require_proposal(context.proposal_id)
            if proposal.status is not ProposalStatus.APPROVED:
                # 审批与应用两阶段分离，只有已审批提案可被应用。
                message = (
                    "only approved proposals can be applied; "
                    f"current status={proposal.status.value}"
                )
                raise InvalidTransition(message)

            version = self._version + 1
            now = datetime.now(UTC)
            # 应用若校验失败，提案标记 FAILED 并保留审计记录，版本不因此回滚。
            try:
                next_state = self._apply_action(proposal.action)
            except (ValidationError, TypeError, ValueError) as error:
                error_text = str(error)[:2000]
                updated = proposal.model_copy(
                    update={
                        "status": ProposalStatus.FAILED,
                        "updated_at": now,
                        "updated_version": version,
                        "error": error_text,
                    },
                    deep=True,
                )
                event = AuditEventType.PROPOSAL_FAILED
                details: dict[str, JsonValue] = {
                    "status": ProposalStatus.FAILED.value,
                    "error": error_text,
                }
            else:
                self._state = next_state
                updated = proposal.model_copy(
                    update={
                        "status": ProposalStatus.APPLIED,
                        "updated_at": now,
                        "updated_version": version,
                        "error": None,
                    },
                    deep=True,
                )
                event = AuditEventType.PROPOSAL_APPLIED
                details = {"status": ProposalStatus.APPLIED.value}

            self._proposals[context.proposal_id] = updated
            self._version = version
            self._append_audit(
                operation_id=context.operation_id,
                event=event,
                proposal=updated,
                actor=context.actor,
                details=details,
            )
            self._remember(context.operation_id, fingerprint, updated)
            return updated.model_copy(deep=True)

    apply_proposal = apply

    def _decide(
        self,
        proposal_id: str,
        *,
        operation_id: str,
        expected_version: int,
        actor: str,
        reason: str | None,
        target: ProposalStatus,
    ) -> Proposal:
        context = _DecisionContext(
            proposal_id=proposal_id,
            operation_id=operation_id,
            expected_version=expected_version,
            actor=actor,
            reason=reason,
        )
        operation = "approve" if target is ProposalStatus.APPROVED else "reject"
        fingerprint = self._fingerprint(
            operation,
            {
                "proposal_id": context.proposal_id,
                "actor": context.actor,
                "reason": context.reason,
            },
        )
        with self._lock:
            replay = self._replay(context.operation_id, fingerprint)
            if replay is not None:
                return replay
            self._require_version(context.expected_version)
            proposal = self._require_proposal(context.proposal_id)
            if proposal.status is not ProposalStatus.PENDING:
                # 状态机单向流转：只有待审批提案能被决定，防止重复审批。
                raise InvalidTransition(
                    f"only pending proposals can be decided; current status={proposal.status.value}"
                )

            version = self._version + 1
            updated = proposal.model_copy(
                update={
                    "status": target,
                    "updated_at": datetime.now(UTC),
                    "updated_version": version,
                    "decided_by": context.actor,
                    "decision_reason": context.reason,
                },
                deep=True,
            )
            self._proposals[context.proposal_id] = updated
            self._version = version
            event = (
                AuditEventType.PROPOSAL_APPROVED
                if target is ProposalStatus.APPROVED
                else AuditEventType.PROPOSAL_REJECTED
            )
            details: dict[str, JsonValue] = {"status": target.value}
            if context.reason is not None:
                details["reason"] = context.reason
            self._append_audit(
                operation_id=context.operation_id,
                event=event,
                proposal=updated,
                actor=context.actor,
                details=details,
            )
            self._remember(context.operation_id, fingerprint, updated)
            return updated.model_copy(deep=True)

    def _apply_action(self, action: Action) -> WorkflowState:
        # 纯数据状态迁移：只更新类型化内存状态，绝不执行任何模型生成的代码。
        if isinstance(action, SetParameterAction):
            if action.path.startswith("experiment."):
                field_name = action.path.removeprefix("experiment.")
                experiment_data = self._state.experiment.model_dump(mode="python")
                experiment_data[field_name] = deepcopy(action.value)
                experiment = ExperimentConfig.model_validate(experiment_data, strict=True)
                return self._state.model_copy(update={"experiment": experiment}, deep=True)
            parameters = deepcopy(self._state.parameters)
            parameters[action.path] = deepcopy(action.value)
            return self._state.model_copy(update={"parameters": parameters}, deep=True)

        if isinstance(action, ChangePolicyAction):
            experiment = self._state.experiment
            if action.policy.startswith("experiment."):
                field_name = action.policy.removeprefix("experiment.")
                if "." in field_name or field_name not in ExperimentConfig.model_fields:
                    raise ValueError(f"unknown experiment policy: {action.policy}")
                experiment_data = experiment.model_dump(mode="python")
                experiment_data[field_name] = deepcopy(action.value)
                experiment = ExperimentConfig.model_validate(experiment_data, strict=True)
            return self._state.model_copy(
                update={
                    "experiment": experiment,
                    "policy": action.policy,
                    "policy_value": deepcopy(action.value),
                },
                deep=True,
            )

        if isinstance(action, EnableResourceAction):
            resources = set(self._state.enabled_resources)
            capacities = dict(self._state.resource_capacities)
            if action.enabled:
                resources.add(action.resource)
                if action.capacity is not None:
                    capacities[action.resource] = action.capacity
            else:
                resources.discard(action.resource)
                capacities.pop(action.resource, None)
            return self._state.model_copy(
                update={
                    "enabled_resources": tuple(sorted(resources)),
                    "resource_capacities": capacities,
                },
                deep=True,
            )

        raise TypeError("unsupported action type")

    def _require_version(self, expected_version: int) -> None:
        if expected_version != self._version:
            raise VersionConflict(expected_version, self._version)

    def _require_proposal(self, proposal_id: str) -> Proposal:
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ProposalNotFound(f"proposal not found: {proposal_id}")
        return proposal

    def _replay(self, operation_id: str, fingerprint: str) -> Proposal | None:
        record = self._operations.get(operation_id)
        if record is None:
            return None
        if record.fingerprint != fingerprint:
            raise IdempotencyConflict(
                f"operation_id {operation_id!r} was already used for a different request"
            )
        return record.result.model_copy(deep=True)

    def _remember(self, operation_id: str, fingerprint: str, proposal: Proposal) -> None:
        self._operations[operation_id] = _OperationRecord(fingerprint, proposal)

    @staticmethod
    def _fingerprint(operation: str, payload: Mapping[str, Any]) -> str:
        return json.dumps(
            {"operation": operation, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def _append_audit(
        self,
        *,
        operation_id: str,
        event: AuditEventType,
        proposal: Proposal,
        actor: str,
        details: dict[str, JsonValue],
    ) -> None:
        self._audit.append(
            AuditEntry(
                sequence=len(self._audit) + 1,
                version=self._version,
                timestamp=datetime.now(UTC),
                operation_id=operation_id,
                event=event,
                proposal_id=proposal.proposal_id,
                actor=actor,
                details=deepcopy(details),
            )
        )


HumanApprovalWorkflow = ApprovalWorkflow
WorkflowEngine = ApprovalWorkflow


__all__ = [
    "Action",
    "ApprovalWorkflow",
    "AuditEntry",
    "AuditEventType",
    "ChangePolicyAction",
    "EnableResourceAction",
    "HumanApprovalWorkflow",
    "IdempotencyConflict",
    "InvalidTransition",
    "Proposal",
    "ProposalNotFound",
    "ProposalStatus",
    "SetParameterAction",
    "VersionConflict",
    "WorkflowEngine",
    "WorkflowError",
    "WorkflowSession",
    "WorkflowState",
    "validate_action",
]
