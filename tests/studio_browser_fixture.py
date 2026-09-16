"""Explicitly started, isolated browser-test server with a deterministic AI fixture.

Run ``python tests/studio_browser_fixture.py`` only for browser integration tests.
This entry point is separate from the real Studio and never contacts a provider.
"""
from __future__ import annotations

import uvicorn

from simlab.studio import create_studio_app
from simlab.studio_ai import AdjustmentProposal


class BrowserTestAgent:
    def propose(self, **kwargs):
        assert "90" in kwargs["prompt"]
        return AdjustmentProposal.model_validate({
            "note": "浏览器自动化测试响应：将第一台设备可用率设置为90%，其余参数保持不变。",
            "changes": [{"path": "machines.0.availability", "value": 0.9}],
        })


def fixture_app():
    application = create_studio_app(
        output_root="outputs/studio_qa/ai_sessions",
        agent_factory=lambda settings: BrowserTestAgent(),
    )

    @application.get("/fixture-health")
    def fixture_health():
        return {"injected_test_agent": True}

    return application


if __name__ == "__main__":
    uvicorn.run(fixture_app(), host="127.0.0.1", port=8876)
