from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from simlab.manufacturing import case_a_config
from simlab.studio_ai import (
    AdjustmentProposal,
    AISettings,
    ManufacturingAdjustmentAgent,
    StudioAIError,
    apply_proposal,
    validate_endpoint,
)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://external.example/v1",
        "https://user:password@example.com/v1",
        "https://example.com/v1?api_key=secret",
        "https://example.com/#secret",
        "file:///secret",
        "https://example.com:bad/v1",
    ],
)
def test_rejects_unsafe_or_secret_bearing_endpoints(endpoint):
    with pytest.raises(StudioAIError):
        validate_endpoint(endpoint)


def test_accepts_explicit_https_and_local_compatible_endpoints():
    assert validate_endpoint(" https://api.example.com/v1/ ") == "https://api.example.com/v1"
    assert validate_endpoint("http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert validate_endpoint("") == ""


def make_agent(monkeypatch, *, base_url=""):
    client = Mock()
    monkeypatch.setattr("simlab.studio_ai.OpenAI", Mock(return_value=client))
    return ManufacturingAdjustmentAgent(
        AISettings(model="test-model", api_key="secret", base_url=base_url)
    ), client


def arguments():
    return {
        "config": case_a_config().model_dump(mode="json"),
        "mode": "paper",
        "prompt": "第一台改90%",
        "context": {},
    }


@pytest.mark.parametrize(
    "base_url", ["", "https://api.openai.com/v1", "https://api.openai.com/v1/"]
)
def test_official_structured_response_uses_no_store(monkeypatch, base_url):
    agent, client = make_agent(monkeypatch, base_url=base_url)
    proposal = AdjustmentProposal(
        note="提高第一台可用率", changes=[{"path": "machines.0.availability", "value": 0.9}]
    )
    client.responses.parse.return_value = SimpleNamespace(
        status="completed", output_parsed=proposal
    )
    assert agent.propose(**arguments()) == proposal
    kwargs = client.responses.parse.call_args.kwargs
    assert kwargs["store"] is False
    assert kwargs["text_format"] is AdjustmentProposal
    assert "secret" not in str(kwargs)
    assert "buffers.0.capacity" not in kwargs["input"][1]["content"]
    assert "machines.0.availability" in kwargs["input"][1]["content"]
    assert "preview_metadata" in kwargs["input"][0]["content"]
    assert "latest_study_summary" in kwargs["input"][0]["content"]
    assert not client.chat.completions.create.called


@pytest.mark.parametrize("status,parsed", [("incomplete", None), ("completed", None)])
def test_official_rejects_incomplete_or_refusal(monkeypatch, status, parsed):
    agent, client = make_agent(monkeypatch)
    client.responses.parse.return_value = SimpleNamespace(status=status, output_parsed=parsed)
    with pytest.raises(StudioAIError):
        agent.propose(**arguments())


def test_compatible_chat_validates_json_and_retains_real_call(monkeypatch):
    agent, client = make_agent(monkeypatch, base_url="https://api.example.com/v1")
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content='{"note":"调整","changes":[{"path":"machines.0.availability","value":0.9}]}'
                ),
            )
        ]
    )
    proposal = agent.propose(**arguments())
    assert proposal.changes[0].value == 0.9
    assert client.chat.completions.create.call_args.kwargs["response_format"] == {
        "type": "json_object"
    }
    assert not client.responses.parse.called


@pytest.mark.parametrize(
    "finish_reason,content",
    [
        ("length", '{"note":"截断","changes":[]}'),
        ("content_filter", '{"note":"过滤","changes":[]}'),
        ("stop", "```python\nprint('bad')\n```"),
        ("stop", '{"note":"extra","changes":[],"code":"bad"}'),
    ],
)
def test_compatible_chat_rejects_partial_or_invalid_response(monkeypatch, finish_reason, content):
    agent, client = make_agent(monkeypatch, base_url="https://api.example.com/v1")
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))
        ]
    )
    with pytest.raises(StudioAIError):
        agent.propose(**arguments())


def test_custom_capacity_integer_conversion_and_no_input_mutation():
    config = case_a_config().model_dump(mode="json")
    proposal = AdjustmentProposal(
        note="增大容量", changes=[{"path": "buffers.0.capacity", "value": 8.0}]
    )
    candidate = apply_proposal(config, proposal, "custom")
    assert candidate["buffers"][0]["capacity"] == 8
    assert type(candidate["buffers"][0]["capacity"]) is int
    assert config["buffers"][0]["capacity"] == 5
    with pytest.raises(StudioAIError):
        apply_proposal(config, proposal, "paper")


@pytest.mark.parametrize(
    "base_url,api_format,responses",
    [
        ("", "chat_completions", False),
        ("https://api.openai.com/v1", "chat_completions", False),
        ("https://compatible.example/v1", "responses", True),
        ("http://localhost:11434/v1", "responses", True),
    ],
)
def test_explicit_protocol_override_uses_selected_api(monkeypatch, base_url, api_format, responses):
    client = Mock()
    monkeypatch.setattr("simlab.studio_ai.OpenAI", Mock(return_value=client))
    agent = ManufacturingAdjustmentAgent(
        AISettings(
            model="selected-model", api_key="private-key", base_url=base_url, api_format=api_format
        )
    )
    proposal = AdjustmentProposal(note="无需调整", changes=[])
    client.responses.parse.return_value = SimpleNamespace(
        status="completed", output_parsed=proposal
    )
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=proposal.model_dump_json())
            )
        ]
    )
    assert agent.propose(**arguments()) == proposal
    assert client.responses.parse.called == responses
    assert client.chat.completions.create.called != responses
    assert agent.settings.audit()["api_format"] == api_format
    assert "private-key" not in str(agent.settings.audit())
