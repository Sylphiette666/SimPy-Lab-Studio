"""LLM 蓝图式模型设计：把自然语言描述转成结构化仿真配置（Model generation 环节）。

模型只输出 SimulationConfig 数据（工位序列、分布类型与参数等），返回后经
本地 Pydantic 严格校验并包装成完整 ProjectConfig；模型不接触代码、不执行
任何东西，生成结果仍需人工 validate/run 验证后才投入使用。

与 agent.py 的差异：本模块生成的是“从零开始的模型蓝图”（结构 + 参数），
agent.py 只对已有模型做白名单内的参数调适；两者共用同一条双提供商路径
（OpenAI Responses 结构化输出 / DeepSeek 兼容 chat-completions + JSON 模式）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)
from pydantic import ValidationError

from simlab.config import ExperimentConfig, ProjectConfig, SimulationConfig


class ModelDesignError(RuntimeError):
    """模型蓝图生成失败时的用户可见错误。"""


class OpenAIModelDesigner:
    """把自然语言需求描述转化为经校验的仿真配置蓝图。"""

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
    ) -> None:
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
                raise ModelDesignError(f"{self.api_key_env} is not set")
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

    def design(
        self,
        description: str,
        *,
        project_name: str = "designed-service-system",
        experiment: ExperimentConfig | None = None,
    ) -> ProjectConfig:
        """生成配置蓝图；返回前通过完整 ProjectConfig 校验。"""

        if not description.strip():
            raise ModelDesignError("需求描述不能为空")
        system_prompt = (
            "你是离散事件仿真建模专家。根据用户的自然语言需求，设计一个"
            "串行多工位排队系统的仿真配置。规则：所有时间量使用用户指定的"
            "业务时间单位；到达间隔与服务时间只能选择 exponential（mean>0）、"
            "deterministic（value>0）、uniform（0<low<high）或 triangular"
            "（0<low<=mode<=high）四种分布；工位名称唯一且反映业务含义；"
            "capacity 为不小于 1 的整数；until 为仿真时长且 warmup 必须小于"
            " until；需求未给出具体数值时给出合理默认值。你只能输出结构化"
            "数据，绝不输出代码或执行任何操作。用中文回答。"
        )
        user_content = f"需求描述：{description}"
        if self.base_url:
            schema_text = json.dumps(
                SimulationConfig.model_json_schema(),
                ensure_ascii=False,
            )
            system_prompt += (
                "\n必须输出一个 JSON 对象（不得使用 Markdown 代码块或任何额外文字），"
                "字段严格符合以下 JSON Schema。"
                f"JSON Schema：{schema_text}"
            )
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
                    text_format=SimulationConfig,
                    max_output_tokens=self.max_output_tokens,
                    store=self.store,
                )
        except AuthenticationError as error:
            raise ModelDesignError(
                f"API 认证失败，请检查 {self.api_key_env}。"
            ) from error
        except RateLimitError as error:
            raise ModelDesignError("API 当前限流或额度不足，请稍后重试。") from error
        except APITimeoutError as error:
            raise ModelDesignError("API 请求超时。") from error
        except APIConnectionError as error:
            raise ModelDesignError("无法连接 API 服务，请检查网络。") from error
        except APIStatusError as error:
            request_id = getattr(error, "request_id", None)
            suffix = f"（request_id={request_id}）" if request_id else ""
            raise ModelDesignError(f"API 返回 HTTP {error.status_code}{suffix}。") from error
        except OpenAIError as error:
            raise ModelDesignError("API 请求失败。") from error

        if self.base_url:
            content = response.choices[0].message.content
            try:
                data = json.loads(content)
            except json.JSONDecodeError as error:
                raise ModelDesignError("模型响应不是有效 JSON。") from error
            try:
                simulation = SimulationConfig.model_validate(data)
            except ValidationError as error:
                raise ModelDesignError("模型蓝图未通过本地结构校验。") from error
        else:
            status = getattr(response, "status", None)
            if status not in {None, "completed"}:
                details = getattr(response, "incomplete_details", None)
                reason = getattr(details, "reason", None)
                suffix = f"：{reason}" if reason else ""
                raise ModelDesignError(f"响应状态为 {status}{suffix}")
            simulation = getattr(response, "output_parsed", None)
            if simulation is None:
                raise ModelDesignError("响应未包含可解析的模型蓝图，可能是拒答或输出不完整。")

        try:
            return ProjectConfig(
                project_name=project_name,
                simulation=simulation,
                experiment=experiment or ExperimentConfig(),
            )
        except ValidationError as error:
            raise ModelDesignError("模型蓝图无法构成有效项目配置。") from error


def save_config_yaml(config: ProjectConfig, output_path: str | Path) -> Path:
    """把校验过的配置写为 YAML 文件。"""

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target


__all__ = [
    "ModelDesignError",
    "OpenAIModelDesigner",
    "save_config_yaml",
]
