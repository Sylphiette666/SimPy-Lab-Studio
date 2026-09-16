from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, AuthenticationError
from pydantic import ValidationError

from simlab.agent import (
    ActionProposal,
    AllowedAction,
    OpenAIProposalAgent,
    ProposalGenerationError,
    ProposedAction,
)


def allowed_action() -> AllowedAction:
    return AllowedAction(
        action_type="set_parameter",
        target="stations.1.capacity",
        description="调整专家台容量",
    )


def proposed_action(*, requires_approval: bool = True) -> ProposedAction:
    return ProposedAction(
        action_type="set_parameter",
        target="stations.1.capacity",
        proposed_value=3,
        rationale="该工位利用率和等待时间较高。",
        expected_effect="预计降低排队等待时间。",
        risk="medium",
        requires_approval=requires_approval,
    )


class FakeResponses:
    def __init__(self, parsed: ActionProposal | None = None, error: Exception | None = None):
        self.parsed = parsed
        self.error = error
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status="completed", output_parsed=self.parsed)


class FakeClient:
    def __init__(self, parsed: ActionProposal | None = None, error: Exception | None = None):
        self.responses = FakeResponses(parsed=parsed, error=error)


def test_proposal_agent_uses_structured_responses_and_sends_only_safe_inputs() -> None:
    expected = ActionProposal(
        summary="建议先小幅增加专家台容量并复验。",
        actions=[proposed_action()],
        caveats=["效果仍需通过新一轮仿真验证。"],
    )
    client = FakeClient(parsed=expected)
    agent = OpenAIProposalAgent(
        model="gpt-test",
        max_output_tokens=700,
        client=client,
    )

    actual = agent.propose(
        current_config={"stations": [{"capacity": 1}, {"capacity": 2}]},
        kpi_summary=[{"metric": "wait", "mean": 8.5}],
        objective="降低等待时间",
        allowed_actions=[allowed_action()],
    )

    assert actual == expected
    kwargs = client.responses.kwargs
    assert kwargs["model"] == "gpt-test"
    assert kwargs["text_format"] is ActionProposal
    assert kwargs["max_output_tokens"] == 700
    assert kwargs["store"] is False
    payload = json.loads(kwargs["input"][1]["content"])
    assert set(payload) == {
        "current_config",
        "kpi_summary",
        "objective",
        "allowed_actions",
    }
    assert payload["objective"] == "降低等待时间"
    assert "replications" not in payload
    assert not hasattr(agent, "execute")
    assert not hasattr(agent, "apply")


def test_proposal_agent_rejects_action_outside_allowlist() -> None:
    proposal = ActionProposal(
        summary="越界建议",
        actions=[
            ProposedAction(
                action_type="enable_resource",
                target="stations.9",
                proposed_value=True,
                rationale="更多资源可能降低等待。",
                expected_effect="等待下降。",
                risk="high",
                requires_approval=True,
            )
        ],
    )
    agent = OpenAIProposalAgent(client=FakeClient(parsed=proposal))

    with pytest.raises(ProposalGenerationError, match="超出允许范围"):
        agent.propose({}, [], [allowed_action()])


def test_proposal_agent_requires_human_approval() -> None:
    proposal = ActionProposal(
        summary="缺少审批标记",
        actions=[proposed_action(requires_approval=False)],
    )
    agent = OpenAIProposalAgent(client=FakeClient(parsed=proposal))

    with pytest.raises(ProposalGenerationError, match="必须要求人工审批"):
        agent.propose({}, [], [allowed_action()])


def test_proposal_models_forbid_extra_fields_and_unknown_action_types() -> None:
    assert proposed_action().requires_approval is True
    with pytest.raises(ValidationError):
        AllowedAction.model_validate(
            {
                "action_type": "set_parameter",
                "target": "x",
                "description": "x",
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        ProposedAction.model_validate(
            {
                "action_type": "run_code",
                "target": "x",
                "proposed_value": "x",
                "rationale": "x",
                "expected_effect": "x",
                "risk": "low",
                "requires_approval": True,
            }
        )
    with pytest.raises(ValidationError):
        ActionProposal.model_validate(
            {"summary": "x", "actions": [], "caveats": [], "unexpected": True}
        )


def test_proposal_agent_requires_key_only_for_real_client(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProposalGenerationError, match="OPENAI_API_KEY"):
        OpenAIProposalAgent()


def test_proposal_agent_rejects_empty_allowlist_before_api_call() -> None:
    client = FakeClient(parsed=ActionProposal(summary="unused"))
    agent = OpenAIProposalAgent(client=client)

    with pytest.raises(ProposalGenerationError, match="allowed_actions"):
        agent.propose({}, [], [])
    assert client.responses.kwargs is None


def test_proposal_agent_rejects_incomplete_response() -> None:
    client = FakeClient(parsed=ActionProposal(summary="unused"))
    client.responses.parse = lambda **kwargs: SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output_parsed=None,
    )
    agent = OpenAIProposalAgent(client=client)

    with pytest.raises(ProposalGenerationError, match="max_output_tokens"):
        agent.propose({}, [], [allowed_action()])


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses")),
            "请求超时",
        ),
        (
            AuthenticationError(
                "invalid key",
                response=httpx.Response(
                    401,
                    request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
                ),
                body=None,
            ),
            "认证失败",
        ),
    ],
)
def test_proposal_agent_maps_openai_errors(error: Exception, message: str) -> None:
    agent = OpenAIProposalAgent(client=FakeClient(error=error))

    with pytest.raises(ProposalGenerationError, match=message):
        agent.propose({}, [], [allowed_action()])
