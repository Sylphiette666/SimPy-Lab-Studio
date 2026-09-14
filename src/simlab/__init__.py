"""SimPy KPI Lab 公开包：离散事件仿真、KPI 统计、AI 分析与人工审批控制。

对外只暴露少数核心入口：配置模型（ProjectConfig）、实验编排（ExperimentRunner）、
审批工作流（ApprovalWorkflow）与控制服务（SimulationControlService）。
"""

from importlib.metadata import PackageNotFoundError, version

from simlab.config import ProjectConfig
from simlab.experiment import ExperimentRunner
from simlab.service import SimulationControlService
from simlab.simulation import run_replication
from simlab.workflow import ApprovalWorkflow

__all__ = [
    "ApprovalWorkflow",
    "ExperimentRunner",
    "ProjectConfig",
    "SimulationControlService",
    "run_replication",
]
try:
    __version__ = version("simpy-kpi-lab")
except PackageNotFoundError:  # pragma: no cover - only when run outside an installation.
    __version__ = "0+unknown"
