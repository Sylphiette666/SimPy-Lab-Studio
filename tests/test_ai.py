from __future__ import annotations

from types import SimpleNamespace

import pytest

from simlab.ai import (
    AIAnalysisError,
    Finding,
    KPIAnalysis,
    OpenAIKPIAnalyst,
    Recommendation,
)


def test_analysis_markdown_rendering() -> None:
    analysis = KPIAnalysis(
        executive_summary="场景 B 的吞吐量更高。",
        best_scenario="B",
        findings=[Finding(title="瓶颈", evidence="利用率为 95%", impact="排队增加")],
        recommendations=[
            Recommendation(
                priority="high",
                action="增加容量",
                rationale="瓶颈利用率过高",
                expected_effect="缩短等待时间",
            )
        ],
        caveats=["这是仿真结果，不是因果证明。"],
    )
    markdown = analysis.to_markdown()
    assert "场景 B" in markdown
    assert "增加容量" in markdown
    assert "因果证明" in markdown


class FakeResponses:
    def __init__(self, parsed: KPIAnalysis):
        self.parsed = parsed
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(status="completed", output_parsed=self.parsed)


class FakeClient:
    def __init__(self, parsed: KPIAnalysis):
        self.responses = FakeResponses(parsed)


def test_openai_adapter_uses_structured_responses_without_real_key() -> None:
    expected = KPIAnalysis(executive_summary="结果稳定。", best_scenario=None)
    client = FakeClient(expected)
    analyst = OpenAIKPIAnalyst(
        model="gpt-test",
        max_output_tokens=800,
        store=False,
        client=client,
    )

    actual = analyst.analyze({"summary": [], "metric_catalog": []})

    assert actual == expected
    assert client.responses.kwargs["model"] == "gpt-test"
    assert client.responses.kwargs["text_format"] is KPIAnalysis
    assert client.responses.kwargs["max_output_tokens"] == 800
    assert client.responses.kwargs["store"] is False


def test_openai_adapter_requires_key_only_when_creating_real_client(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(AIAnalysisError, match="OPENAI_API_KEY"):
        OpenAIKPIAnalyst()


def test_openai_adapter_rejects_incomplete_response() -> None:
    expected = KPIAnalysis(executive_summary="unused", best_scenario=None)
    client = FakeClient(expected)
    client.responses.parse = lambda **kwargs: SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output_parsed=None,
    )
    analyst = OpenAIKPIAnalyst(client=client)

    with pytest.raises(AIAnalysisError, match="max_output_tokens"):
        analyst.analyze({"summary": []})
