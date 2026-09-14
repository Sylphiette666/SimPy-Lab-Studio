"""项目配置模型：仿真、实验与 OpenAI 三段式 YAML 配置。

全部模型启用 extra="forbid"，未知字段会在加载时直接报错，避免拼写错误被静默忽略。
"""
from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DistributionConfig(BaseModel):
    """Supported duration/interarrival distributions, expressed in model time units."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["exponential", "deterministic", "uniform", "triangular"]
    mean: float | None = None
    value: float | None = None
    low: float | None = None
    high: float | None = None
    mode: float | None = None

    @model_validator(mode="after")
    def validate_parameters(self) -> DistributionConfig:
        """按分布类型校验所需参数齐备且取值范围合法。"""

        if self.kind == "exponential" and (self.mean is None or self.mean <= 0):
            raise ValueError("exponential distribution requires mean > 0")
        if self.kind == "deterministic" and (self.value is None or self.value < 0):
            raise ValueError("deterministic distribution requires value >= 0")
        if self.kind == "uniform":
            if self.low is None or self.high is None or self.low > self.high:
                raise ValueError("uniform distribution requires low <= high")
            if self.low < 0:
                raise ValueError("uniform distribution requires low >= 0")
        if self.kind == "triangular":
            if self.low is None or self.high is None or self.mode is None:
                raise ValueError("triangular distribution requires low, mode and high")
            if not self.low <= self.mode <= self.high:
                raise ValueError("triangular distribution requires low <= mode <= high")
            if self.low < 0:
                raise ValueError("triangular distribution requires low >= 0")
        return self

    def sample(self, rng: random.Random) -> float:
        """按分布类型从 rng 采样一个时长值。"""

        if self.kind == "exponential":
            return rng.expovariate(1.0 / float(self.mean))
        if self.kind == "deterministic":
            return float(self.value)
        if self.kind == "uniform":
            return rng.uniform(float(self.low), float(self.high))
        return rng.triangular(float(self.low), float(self.high), float(self.mode))


class StationConfig(BaseModel):
    """单个工位：名称、容量与服务时间分布。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    capacity: int = Field(default=1, ge=1)
    service_time: DistributionConfig


class SimulationConfig(BaseModel):
    """一次仿真的运行参数：时长、预热、到达过程与工位序列。"""

    model_config = ConfigDict(extra="forbid")

    name: str = "service_system"
    until: float = Field(gt=0)
    warmup: float = Field(default=0, ge=0)
    first_arrival_at_zero: bool = True
    max_arrivals: int | None = Field(default=None, ge=1)
    arrival_interarrival: DistributionConfig
    stations: list[StationConfig] = Field(min_length=1)
    cycle_time_target: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_simulation(self) -> SimulationConfig:
        """校验预热期、工位命名唯一性与到达间隔约束。"""

        if self.warmup >= self.until:
            raise ValueError("warmup must be smaller than until")
        names = [station.name for station in self.stations]
        if len(names) != len(set(names)):
            raise ValueError("station names must be unique")
        arrival = self.arrival_interarrival
        if arrival.kind == "deterministic" and arrival.value == 0:
            raise ValueError("deterministic interarrival value must be > 0")
        if arrival.kind in {"uniform", "triangular"} and arrival.low == 0:
            raise ValueError("interarrival distribution must have strictly positive support")
        return self


class ExperimentConfig(BaseModel):
    """实验设计：重复次数、随机数策略、参数网格与输出目录。"""

    model_config = ConfigDict(extra="forbid")

    replications: int = Field(default=10, ge=1)
    base_seed: int = Field(default=20260825, ge=0)
    common_random_numbers: bool = True
    confidence_level: float = Field(default=0.95, gt=0, lt=1)
    parameter_grid: dict[str, list[Any]] = Field(default_factory=dict)
    output_dir: str = "outputs"
    # 是否逐实体记录事件轨迹，并在保存结果时导出 traces.xes / traces.jsonl。
    record_traces: bool = False

    @model_validator(mode="after")
    def validate_grid(self) -> ExperimentConfig:
        """参数网格的每个键都必须是可展开的非空列表。"""

        empty = [key for key, values in self.parameter_grid.items() if not values]
        if empty:
            raise ValueError(f"parameter_grid entries cannot be empty: {empty}")
        return self


class OpenAIConfig(BaseModel):
    """OpenAI 调用参数；设置 base_url 可切换到 DeepSeek 等兼容服务。"""

    model_config = ConfigDict(extra="forbid")

    model: str = "gpt-5.6"
    max_output_tokens: int = Field(default=2500, ge=256)
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    store: bool = False
    # 兼容 DeepSeek 等 OpenAI 协议服务：设置后走 chat/completions + JSON 模式。
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"


class ProjectConfig(BaseModel):
    """项目级配置：仿真、实验与 OpenAI 三段合一。"""

    model_config = ConfigDict(extra="forbid")

    project_name: str = "simpy-kpi-lab"
    simulation: SimulationConfig
    experiment: ExperimentConfig = Field(default_factory=ExperimentConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)

    @classmethod
    def load(cls, path: str | Path) -> ProjectConfig:
        """从 YAML 文件加载配置；SIMLAB_OPENAI_* 环境变量优先级高于文件。"""

        config_path = Path(path)
        with config_path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
        if not isinstance(data, dict):
            raise ValueError(f"configuration root must be a mapping: {config_path}")
        config = cls.model_validate(data)
        # 环境变量优先级高于配置文件，便于在共享配置文件上切换模型。
        env_model = os.getenv("SIMLAB_OPENAI_MODEL")
        if env_model:
            config.openai.model = env_model
        env_base_url = os.getenv("SIMLAB_OPENAI_BASE_URL")
        if env_base_url:
            config.openai.base_url = env_base_url
        return config
