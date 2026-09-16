"""Real SDK parsing against in-memory HTTP responses; no provider requests."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from simlab.manufacturing import case_a_config
from simlab.studio import create_studio_app
from simlab.studio_ai import AISettings, ManufacturingAdjustmentAgent, StudioAIError

PROPOSAL = {
    "note": "试验提高首台设备可用率，改善须由仿真验证。",
    "changes": [
        {"path": "machines.0.availability", "value": 0.9},
    ],
}


def response_body(*, status="completed", reason=None, content=None):
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "model": "deepseek-v4-flash",
        "status": status,
        "incomplete_details": {"reason": reason} if reason else None,
        "output": [
            {
                "type": "reasoning",
                "id": "rs_test",
                "summary": [],
                "content": [{"type": "reasoning_text", "text": "not parameter data"}],
            },
            {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "status": "completed",
                "content": content
                if content is not None
                else [
                    {
                        "type": "output_text",
                        "text": json.dumps(PROPOSAL),
                        "annotations": [],
                    }
                ],
            },
        ],
    }


def invoke(body, *, protocol="responses", endpoint="https://api.deepseek.com/v1", code=200,
           model="deepseek-v4-flash"):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(code, json=body)

    settings = AISettings(
        model=model,
        api_key="sk-private-test-key",
        base_url=endpoint,
        api_format=protocol,
    )
    agent = ManufacturingAdjustmentAgent(settings)
    agent.client.close()
    with OpenAI(
        api_key=settings.api_key,
        base_url=endpoint,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        agent.client = client
        try:
            result = agent.propose(
                config=case_a_config().model_dump(mode="json"),
                prompt="提高产出率",
                mode="paper",
                context={},
            )
        except StudioAIError as exc:
            result = exc
    assert len(requests) == 1  # No hidden recovery calls or protocol switching.
    return result, requests[0], json.loads(requests[0].content)


def test_deepseek_responses_accepts_final_json_and_disables_default_thinking():
    result, request, payload = invoke(response_body())
    assert result.model_dump() == PROPOSAL
    assert request.url.path == "/v1/responses"
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["max_output_tokens"] == 8192
    assert payload["store"] is False
    schema = payload["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["ParameterChange"]["additionalProperties"] is False


def test_reasoning_only_is_not_treated_as_a_parameter_proposal():
    body = response_body()
    body["output"] = body["output"][:1]
    body["output"][0]["content"][0]["text"] = json.dumps(PROPOSAL)
    result, _, _ = invoke(body)
    assert isinstance(result, StudioAIError)
    assert "未返回参数正文" in str(result)


def test_incomplete_message_inside_completed_response_is_rejected():
    body = response_body()
    body["output"][1]["status"] = "incomplete"
    result, _, _ = invoke(body)
    assert isinstance(result, StudioAIError)
    assert "未完成响应" in str(result)


@pytest.mark.parametrize(
    "content",
    [
        [],
        [{"type": "output_text", "text": '{"note":"unfinished', "annotations": []}],
        [{"type": "output_text", "text": json.dumps(PROPOSAL), "annotations": []}],
    ],
)
def test_truncation_is_diagnosed_before_parsing_even_if_json_looks_valid(content):
    result, _, _ = invoke(
        response_body(status="incomplete", reason="max_output_tokens", content=content)
    )
    assert isinstance(result, StudioAIError)
    assert "长度上限" in str(result)
    assert "结构校验" not in str(result)


@pytest.mark.parametrize(
    "body,expected",
    [
        (response_body(content=[]), "未返回参数正文"),
        (response_body(status="incomplete", reason="content_filter"), "过滤"),
        (response_body(status="failed"), "未完成响应"),
        (response_body(content=[{"type": "refusal", "refusal": "sk-private-test-key"}]), "拒绝"),
        (
            response_body(
                content=[
                    {
                        "type": "output_text",
                        "text": '{"note":"extra","changes":[],"code":"bad"}',
                        "annotations": [],
                    }
                ]
            ),
            "结构校验",
        ),
        (
            response_body(
                content=[{"type": "output_text", "text": "```json\n{}\n```", "annotations": []}]
            ),
            "结构校验",
        ),
    ],
)
def test_failed_output_is_actionable_and_never_echoes_provider_body(body, expected):
    result, _, _ = invoke(body)
    assert isinstance(result, StudioAIError)
    assert expected in str(result)
    assert "sk-private" not in str(result)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.openai.com/v1",
        "https://proxy.example/v1",
        "https://api.deepseek.com.example/v1",
    ],
)
def test_model_name_does_not_send_deepseek_options_to_other_providers(endpoint):
    result, request, payload = invoke(response_body(), endpoint=endpoint)
    assert result.model_dump() == PROPOSAL
    assert str(request.url).startswith(endpoint)
    assert "reasoning" not in payload


@pytest.mark.parametrize(
    "finish,content,expected",
    [
        ("stop", json.dumps(PROPOSAL), None),
        ("length", json.dumps(PROPOSAL), "长度上限"),
        ("stop", "", "未返回参数正文"),
        ("content_filter", "", "过滤"),
    ],
)
@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-flash"])
def test_deepseek_chat_protocol_uses_non_thinking_json(finish, content, expected, model):
    body = {
        "id": "chat_test",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    result, request, payload = invoke(body, protocol="chat_completions", model=model)
    assert payload["model"] == model
    assert request.url.path == "/v1/chat/completions"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 8192
    if expected:
        assert isinstance(result, StudioAIError)
        assert expected in str(result)
    else:
        assert result.model_dump() == PROPOSAL


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422, 429, 500])
def test_http_errors_remain_sanitized(code):
    result, _, _ = invoke(
        {"error": {"message": "sk-private-test-key", "type": "provider"}}, code=code
    )
    assert isinstance(result, StudioAIError)
    assert "sk-private" not in str(result)


def test_truncated_adjustment_preserves_session_and_next_request_can_succeed(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        body = response_body(status="incomplete", reason="max_output_tokens")
        return httpx.Response(200, json=body if len(calls) == 1 else response_body())

    with OpenAI(
        api_key="test-key",
        base_url="https://api.deepseek.com/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as provider:

        def factory(settings):
            agent = ManufacturingAdjustmentAgent(settings)
            agent.client.close()
            agent.client = provider
            return agent

        with TestClient(create_studio_app(output_root=tmp_path, agent_factory=factory)) as client:
            saved = client.put(
                "/api/studio/ai/profiles/default",
                json={
                    "name": "DeepSeek 测试",
                    "model": "deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "test-key",
                    "api_format": "responses",
                },
            )
            assert saved.status_code == 200
            session = client.post(
                "/api/studio/sessions",
                json={
                    "mode": "paper",
                    "config": case_a_config(
                        until_seconds=7200,
                        warmup_seconds=600,
                        replications=2,
                    ).model_dump(mode="json"),
                },
            ).json()
            endpoint = f"/api/studio/sessions/{session['id']}"
            body = {"prompt": "提高产出率", "expected_version_id": session["active_version_id"]}
            failed = client.post(endpoint + "/adjust", json=body)
            assert failed.status_code == 422
            assert "长度上限" in failed.json()["detail"]
            assert client.get(endpoint).json() == session
            success = client.post(endpoint + "/adjust", json=body)
            assert success.status_code == 200
            updated = success.json()
            assert len(updated["versions"]) == 2
            assert updated["versions"][-1]["config"]["machines"][0]["availability"] == 0.9
            assert updated["runs"][-1]["kind"] == "preview"
            assert len(calls) == 2
