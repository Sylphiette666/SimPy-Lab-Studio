"""可选的大模型 KPI 分析适配器。

把实验结果摘要交给 OpenAI（或 DeepSeek 等兼容端点）生成结构化 KPI 分析，
并输出 JSON 与 Markdown 报告；没有可用 API 密钥时本模块可整体跳过。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)
from pydantic import BaseModel, Field, ValidationError


class Finding(BaseModel):
    """单条关键发现：标题、证据与影响。"""

    title: str
    evidence: str
    impact: str


class Recommendation(BaseModel):
    """一条行动建议：优先级、动作、理由与预期效果。"""

    priority: Literal["high", "medium", "low"]
    action: str
    rationale: str
    expected_effect: str


class KPIAnalysis(BaseModel):
    """LLM 生成的结构化 KPI 分析结果。"""

    executive_summary: str
    best_scenario: str | None
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    def to_markdown(self) -> str:
        lines = ["# AI KPI 分析", "", self.executive_summary, ""]
        if self.best_scenario:
            lines.extend([f"**建议场景：** `{self.best_scenario}`", ""])
        lines.extend(["## 关键发现", ""])
        for finding in self.findings:
            lines.extend(
                [f"### {finding.title}", "", finding.evidence, "", f"影响：{finding.impact}", ""]
            )
        lines.extend(["## 建议", ""])
        for item in self.recommendations:
            lines.extend(
                [
                    f"- **[{item.priority}] {item.action}** — "
                    f"{item.rationale}；预期：{item.expected_effect}",
                    "",
                ]
            )
        lines.extend(["## 注意事项", ""])
        lines.extend(f"- {caveat}" for caveat in self.caveats)
        lines.append("")
        return "\n".join(lines)


class AIAnalysisError(RuntimeError):
    """A safe, user-facing failure raised by the optional OpenAI adapter."""


class OpenAIKPIAnalyst:
    """把仿真结果摘要交给大模型生成 KPI 分析。

    官方 OpenAI 端点走 Responses API 的结构化输出；设置 base_url（如 DeepSeek）
    时降级为 chat/completions + JSON 模式。
    """

    def __init__(
        self,
        model: str = "gpt-5.6",
        max_output_tokens: int = 2500,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        store: bool = False,
        base_url: str | None = None,
        api_key_env: str | None = None,
        client: Any | None = None,
    ):
        # base_url 非空（如 DeepSeek）时走 chat/completions + JSON 模式。
        self.base_url = base_url or os.getenv("SIMLAB_OPENAI_BASE_URL")
        self.api_key_env = api_key_env or "OPENAI_API_KEY"
        if (
            self.base_url
            and "deepseek" in self.base_url.lower()
            and self.api_key_env == "OPENAI_API_KEY"
        ):
            # 未显式指定密钥环境变量时，DeepSeek 端点自动改用 DEEPSEEK_API_KEY。
            self.api_key_env = "DEEPSEEK_API_KEY"
        if client is None:
            api_key = os.getenv(self.api_key_env)
            if not api_key:
                raise AIAnalysisError(f"{self.api_key_env} is not set")
            client = OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=timeout_seconds,
                max_retries=max_retries,
            )
        self.client = client
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.store = store

    def analyze(self, results: dict, question: str | None = None) -> KPIAnalysis:
        """基于实验结果生成结构化 KPI 分析；SDK 异常统一转为 AIAnalysisError。"""

        compact_payload = {
            "project_name": results.get("project_name"),
            "simulation": results.get("config", {}).get("simulation"),
            "experiment": results.get("config", {}).get("experiment"),
            "random_streams": results.get("random_streams"),
            "metric_catalog": results.get("metric_catalog"),
            "summary": results.get("summary"),
        }
        user_question = question or (
            "比较所有实验场景，指出 KPI 权衡、瓶颈、最佳场景和下一步实验建议。"
        )
        system_prompt = (
            "你是离散事件仿真与运营研究专家。只根据提供的数据下结论；"
            "明确区分观测、推断和不确定性；不要伪造因果关系。"
            "若数据未提供成本、目标或决策门槛，不得臆断最佳方案，"
            "应将 best_scenario 设为 null 并说明缺失信息。用中文回答。"
        )
        user_content = (
            f"问题：{user_question}\n\n"
            f"实验数据：{json.dumps(compact_payload, ensure_ascii=False)}"
        )
        if self.base_url:
            system_prompt += (
                "\n必须输出一个 JSON 对象（不得使用 Markdown 代码块或任何额外文字），"
                "字段严格符合以下 JSON Schema，缺失或不确定的字段填 null 或空数组。"
                f"JSON Schema：{json.dumps(KPIAnalysis.model_json_schema(), ensure_ascii=False)}"
            )
        # 将 SDK 异常分类翻译成面向用户的中文错误，避免向用户泄漏内部细节。
        try:
            if self.base_url:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=self.max_output_tokens,
                )
            else:
                response = self.client.responses.parse(
                    model=self.model,
                    input=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    text_format=KPIAnalysis,
                    max_output_tokens=self.max_output_tokens,
                    store=self.store,
                )
        except AuthenticationError as error:
            raise AIAnalysisError(
                f"API 认证失败，请检查 {self.api_key_env}。"
            ) from error
        except RateLimitError as error:
            raise AIAnalysisError("API 当前限流或额度不足，请稍后重试。") from error
        except APITimeoutError as error:
            raise AIAnalysisError("API 请求超时。") from error
        except APIConnectionError as error:
            raise AIAnalysisError("无法连接 API 服务，请检查网络。") from error
        except APIStatusError as error:
            request_id = getattr(error, "request_id", None)
            suffix = f"（request_id={request_id}）" if request_id else ""
            raise AIAnalysisError(f"API 返回 HTTP {error.status_code}{suffix}。") from error
        except OpenAIError as error:
            raise AIAnalysisError("API 请求失败。") from error

        if self.base_url:
            content = response.choices[0].message.content
            try:
                data = json.loads(content)
            except json.JSONDecodeError as error:
                raise AIAnalysisError("模型响应不是有效 JSON。") from error
            try:
                return KPIAnalysis.model_validate(data)
            except ValidationError as error:
                raise AIAnalysisError("模型分析未通过本地结构校验。") from error

        status = getattr(response, "status", None)
        if status not in {None, "completed"}:
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None)
            suffix = f"：{reason}" if reason else ""
            raise AIAnalysisError(f"OpenAI 响应状态为 {status}{suffix}")
        if response.output_parsed is None:
            raise AIAnalysisError("OpenAI 响应未包含可解析的 KPI 分析，可能是拒答或输出不完整。")
        return response.output_parsed


def save_analysis(analysis: KPIAnalysis, output_dir: str | Path) -> tuple[Path, Path]:
    """把分析结果写入 output_dir 下的 ai_analysis.json 与 ai_analysis.md。"""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "ai_analysis.json"
    markdown_path = root / "ai_analysis.md"
    with json_path.open("w", encoding="utf-8") as stream:
        json.dump(analysis.model_dump(mode="json"), stream, ensure_ascii=False, indent=2)
    with markdown_path.open("w", encoding="utf-8") as stream:
        stream.write(analysis.to_markdown())
    return json_path, markdown_path
