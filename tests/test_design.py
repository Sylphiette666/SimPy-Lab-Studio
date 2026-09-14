from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from simlab.config import SimulationConfig
from simlab.design import ModelDesignError, OpenAIModelDesigner, save_config_yaml

VALID_SIMULATION = {
    "name": "designed-desk",
    "until": 480,
    "warmup": 30,
    "first_arrival_at_zero": True,
    "arrival_interarrival": {"kind": "exponential", "mean": 6.0},
    "stations": [
        {
            "name": "front-desk",
            "capacity": 2,
            "service_time": {"kind": "triangular", "low": 3.0, "mode": 6.0, "high": 10.0},
        }
    ],
    "cycle_time_target": 25.0,
}


class FakeResponses:
    def __init__(self, parsed: SimulationConfig | None):
        self.parsed = parsed
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(status="completed", output_parsed=self.parsed)


class FakeResponsesClient:
    def __init__(self, parsed: SimulationConfig | None):
        self.responses = FakeResponses(parsed)


class FakeChat:
    class Completions:
        def __init__(self, content: str):
            self.content = content

        def create(self, **kwargs):
            message = SimpleNamespace(content=self.content)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class Chat:
        def __init__(self, content: str):
            self.completions = FakeChat.Completions(content)

    def __init__(self, content: str):
        self.chat = self.Chat(content)


def test_designer_uses_structured_responses_path() -> None:
    expected = SimulationConfig.model_validate(VALID_SIMULATION)
    designer = OpenAIModelDesigner(model="gpt-test", client=FakeResponsesClient(expected))
    config = designer.design("两个工位的服务中心", project_name="demo")
    assert config.project_name == "demo"
    assert config.simulation.name == "designed-desk"
    assert config.simulation.stations[0].capacity == 2
    kwargs = designer.client.responses.kwargs
    assert kwargs["model"] == "gpt-test"
    assert kwargs["text_format"] is SimulationConfig


def test_designer_compat_chat_path_parses_json() -> None:
    designer = OpenAIModelDesigner(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        client=FakeChat(json.dumps(VALID_SIMULATION, ensure_ascii=False)),
    )
    config = designer.design("两个工位的服务中心")
    assert config.simulation.until == 480
    assert config.simulation.stations[0].name == "front-desk"


def test_designer_compat_path_rejects_invalid_schema() -> None:
    designer = OpenAIModelDesigner(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        client=FakeChat('{"until": -1, "stations": []}'),
    )
    with pytest.raises(ModelDesignError, match="结构校验"):
        designer.design("无效蓝图测试")


def test_designer_rejects_non_json_compat_response() -> None:
    designer = OpenAIModelDesigner(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        client=FakeChat("这不是 JSON"),
    )
    with pytest.raises(ModelDesignError, match="不是有效 JSON"):
        designer.design("无效蓝图测试")


def test_designer_requires_description() -> None:
    designer = OpenAIModelDesigner(model="gpt-test", client=FakeResponsesClient(None))
    with pytest.raises(ModelDesignError, match="描述不能为空"):
        designer.design("   ")


def test_designer_requires_key_when_building_real_client(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("SIMLAB_OPENAI_BASE_URL", raising=False)
    with pytest.raises(ModelDesignError, match="OPENAI_API_KEY"):
        OpenAIModelDesigner()


def test_save_config_yaml_roundtrip(tmp_path) -> None:
    designer = OpenAIModelDesigner(
        model="gpt-test",
        client=FakeResponsesClient(SimulationConfig.model_validate(VALID_SIMULATION)),
    )
    config = designer.design("测试", project_name="roundtrip")
    path = save_config_yaml(config, tmp_path / "designed.yaml")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "roundtrip" in text
    assert "front-desk" in text
