"""命令行入口：校验配置、批量跑实验、运行制造案例、调用 AI 分析或启动控制 API。

所有异常统一在 main 中转为中文错误提示并返回退出码 2。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from simlab.ai import OpenAIKPIAnalyst, save_analysis
from simlab.config import ProjectConfig
from simlab.experiment import ExperimentRunner, expand_scenarios


def build_parser() -> argparse.ArgumentParser:
    """构建 simlab 命令的参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="simlab",
        description="SimPy 多实验、KPI 统计与 OpenAI 分析框架",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="校验 YAML 配置")
    validate.add_argument("config", type=Path)

    run = subparsers.add_parser("run", help="执行参数场景与多次 replication")
    run.add_argument("config", type=Path)
    run.add_argument("--output", type=Path, help="覆盖配置中的输出目录")
    run.add_argument("--workers", type=int, default=1, help="并行进程数，默认 1")
    run.add_argument("--analyze", action="store_true", help="完成后调用 AI 分析")
    run.add_argument("--plot", action="store_true", help="完成后生成图表与 HTML 报告")
    run.add_argument("--question", help="给 AI 分析器的业务问题")
    run.add_argument("--model", help="覆盖 AI 模型")

    case_a = subparsers.add_parser(
        "case-a", help="运行论文案例 A 的制造线基准与 V2/V3 改进对比"
    )
    case_a.add_argument(
        "--days", "--horizon-days", dest="days", type=float, default=30,
        help="总仿真时长（天，包含预热期），默认 30",
    )
    case_a.add_argument(
        "--warmup-days", type=float, default=1, help="预热时长（天），默认 1"
    )
    case_a.add_argument(
        "--replications", "--reps", dest="replications", type=int, default=25,
        help="每个场景的独立重复次数，默认 25",
    )
    case_a.add_argument("--seed", type=int, help="覆盖案例配置的随机种子")
    case_a.add_argument("--workers", type=int, default=1, help="并行进程数，默认 1")
    case_a.add_argument(
        "--output", type=Path, default=Path("outputs/case_a"),
        help="输出目录，默认 outputs/case_a",
    )
    case_a.add_argument(
        "--include-infeasible-v1", action="store_true",
        help="额外运行违反 CPD 约束的 V1，仅作论文诊断，不作为可接受方案",
    )

    analyze = subparsers.add_parser("analyze", help="用 AI 分析已有 results.json")
    analyze.add_argument("results", type=Path)
    analyze.add_argument("--question", help="希望模型回答的问题")
    analyze.add_argument("--model", help="覆盖结果配置中的模型")
    analyze.add_argument("--output", type=Path, help="分析文件输出目录")

    plot = subparsers.add_parser("plot", help="为已有 results.json 生成图表与 HTML 报告")
    plot.add_argument("results", type=Path)
    plot.add_argument("--output", type=Path, help="图表输出目录，默认结果目录下 plots/")
    plot.add_argument("--max-cases", type=int, default=12, help="轨迹甘特图最多显示的实体数")

    mine = subparsers.add_parser("mine", help="从事件日志（XES/JSONL）挖掘仿真模型配置")
    mine.add_argument("log", type=Path, help="事件日志文件（.xes 或 .jsonl）")
    mine.add_argument("--output", type=Path, help="输出 YAML 路径")
    mine.add_argument("--project-name", default="mined-service-system", help="项目名称")
    mine.add_argument("--warmup-ratio", type=float, default=0.1, help="预热期占时长的比例")

    design = subparsers.add_parser("design", help="用 LLM 从自然语言描述生成模型蓝图")
    design.add_argument("description", help="需求描述；以 @ 开头时读取该文件内容")
    design.add_argument("--output", type=Path, help="输出 YAML 路径")
    design.add_argument("--project-name", default="designed-service-system", help="项目名称")
    design.add_argument("--model", help="覆盖 LLM 模型")

    serve = subparsers.add_parser("serve", help="启动人工审批与仿真控制 API")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    serve.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    serve.add_argument("--reload", action="store_true", help="开发时自动重载")

    studio = subparsers.add_parser("studio", help="启动中文制造仿真实验应用")
    studio.add_argument("--host", default="127.0.0.1", help="本机监听地址")
    studio.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    studio.add_argument(
        "--output", type=Path, default=Path("outputs/studio"), help="会话与运行结果保存目录"
    )
    studio.add_argument("--open-browser", action="store_true", help="启动后打开浏览器")
    return parser


def analyze_results(
    result: dict,
    output_dir: Path,
    model_override: str | None = None,
    question: str | None = None,
) -> None:
    """用 OpenAI 分析已有结果并保存 JSON/Markdown 报告。"""

    openai_config = result.get("config", {}).get("openai", {})
    model = model_override or openai_config.get("model", "gpt-5.6")
    max_tokens = openai_config.get("max_output_tokens", 2500)
    print(f"正在使用 {model} 分析 KPI 汇总……")
    analysis = OpenAIKPIAnalyst(
        model=model,
        max_output_tokens=max_tokens,
        timeout_seconds=openai_config.get("timeout_seconds", 60.0),
        max_retries=openai_config.get("max_retries", 2),
        store=openai_config.get("store", False),
        base_url=openai_config.get("base_url"),
        api_key_env=openai_config.get("api_key_env"),
    ).analyze(result, question=question)
    json_path, markdown_path = save_analysis(analysis, output_dir)
    print(f"AI 分析已写入 {json_path} 和 {markdown_path}")


def _plot_results(results_path: Path, output_dir: Path, *, max_cases: int = 12) -> None:
    """生成图表与 HTML 报告；matplotlib 为可选依赖，缺失时给出安装提示。"""

    if max_cases < 1:
        raise ValueError("--max-cases must be at least 1")
    try:
        from simlab.viz import load_results, plot_results
    except ImportError as error:
        raise RuntimeError(
            '图表依赖缺失；请先安装：pip install -e ".[viz]"'
        ) from error
    products = plot_results(
        load_results(results_path),
        output_dir,
        max_cases=max_cases,
    )
    print(f"图表已生成（{len(products)} 个文件）：")
    for product in products:
        print(f"  {product.resolve()}")


def main(argv: list[str] | None = None) -> None:
    """CLI 入口；参数错误与运行错误打印到 stderr 后以退出码 2 结束。"""

    args = build_parser().parse_args(argv)
    try:
        if args.command == "studio":
            from simlab.studio_cli import run_studio

            run_studio(
                host=args.host, port=args.port, output_root=args.output,
                open_browser=args.open_browser,
            )
            return

        if args.command == "serve":
            # 端口合法性在启动前拦截，避免 uvicorn 绑定失败才报错。
            if not 1 <= args.port <= 65535:
                raise ValueError("--port must be between 1 and 65535")
            try:
                from simlab.api import run_api
            except ImportError as error:
                raise RuntimeError(
                    'API dependencies are missing; install with pip install -e ".[api]"'
                ) from error
            run_api(host=args.host, port=args.port, reload=args.reload)
            return

        if args.command == "case-a":
            if not math.isfinite(args.days) or args.days <= 0:
                raise ValueError("--days 必须是有限的正数")
            if (
                not math.isfinite(args.warmup_days)
                or args.warmup_days < 0
                or args.warmup_days >= args.days
            ):
                raise ValueError("--warmup-days 必须是有限的非负数，且小于 --days")
            if args.replications < 1:
                raise ValueError("--replications 必须至少为 1")
            if args.workers < 1:
                raise ValueError("--workers 必须至少为 1")

            if args.seed is not None and args.seed < 0:
                raise ValueError("--seed 必须是非负整数")

            from simlab.manufacturing import (
                case_a_config,
                run_manufacturing_study,
                save_manufacturing_study,
            )

            overrides = {
                "until_seconds": args.days * 86400,
                "warmup_seconds": args.warmup_days * 86400,
                "replications": args.replications,
            }
            if args.seed is not None:
                overrides["base_seed"] = args.seed
            config = case_a_config(**overrides)
            scenario_count = 4 if args.include_infeasible_v1 else 3
            print(
                f"开始案例 A：{scenario_count} 个场景，每场景 {args.replications} 次，"
                f"总时长 {args.days:g} 天，预热 {args.warmup_days:g} 天……"
            )
            if args.include_infeasible_v1:
                print("V1 违反 CPD 约束，仅作诊断；不属于可接受的改进方案。")
            result = run_manufacturing_study(
                config,
                include_infeasible=args.include_infeasible_v1,
                workers=args.workers,
            )
            output_dir = save_manufacturing_study(result, args.output)
            print(f"完成。案例 A 结果与对比报告已写入 {output_dir.resolve()}")
            return

        if args.command == "validate":
            config = ProjectConfig.load(args.config)
            scenarios = expand_scenarios(config)
            total = len(scenarios) * config.experiment.replications
            print(
                f"配置有效：{len(scenarios)} 个场景，"
                f"每场景 {config.experiment.replications} 次，共 {total} 次仿真。"
            )
            return

        if args.command == "run":
            if args.workers < 1:
                raise ValueError("--workers must be at least 1")
            config = ProjectConfig.load(args.config)
            runner = ExperimentRunner(config)
            tasks = len(expand_scenarios(config)) * config.experiment.replications
            print(f"开始执行 {tasks} 次仿真（workers={args.workers}）……")
            result = runner.run(workers=args.workers)
            output_dir = runner.save(result, args.output)
            print(f"完成。结果已写入 {output_dir.resolve()}")
            if args.plot:
                _plot_results(output_dir / "results.json", output_dir / "plots")
            if args.analyze:
                analyze_results(result, output_dir, args.model, args.question)
            return

        if args.command == "plot":
            _plot_results(
                args.results,
                args.output or args.results.parent / "plots",
                max_cases=args.max_cases,
            )
            return

        if args.command == "mine":
            from simlab.mining import generate_yaml, read_log

            events = read_log(args.log)
            output, report = generate_yaml(
                events,
                args.output or f"outputs/{args.project_name}.yaml",
                project_name=args.project_name,
                warmup_ratio=args.warmup_ratio,
            )
            print(
                f"已从 {len(events)} 个事件（{report['cases']} 个实体）挖掘出模型，"
                f"工位序列：{' → '.join(report['station_order'])}"
                f"（覆盖率 {report['order_coverage']:.1%}）"
            )
            for name, stats in report["station_stats"].items():
                print(
                    f"  工位 {name}：容量 {stats['capacity']}，"
                    f"分布 {stats['distribution']}，样本 {stats['samples']}，"
                    f"均值 {stats['mean']}"
                )
            for warning in report["warnings"]:
                print(f"  ⚠ {warning}")
            print(f"配置已写入 {output.resolve()}（请先 validate 校验后再运行）")
            return

        if args.command == "design":
            from simlab.design import OpenAIModelDesigner, save_config_yaml

            description = args.description
            if description.startswith("@") and Path(description[1:]).exists():
                description = Path(description[1:]).read_text(encoding="utf-8")
            model = args.model or os.getenv("SIMLAB_OPENAI_MODEL") or "gpt-5.6"
            base_url = os.getenv("SIMLAB_OPENAI_BASE_URL")
            if base_url and "deepseek" in base_url.lower() and model == "gpt-5.6":
                # DeepSeek 端点不接受占位模型名，自动换成 deepseek-chat。
                model = "deepseek-chat"
            print(f"正在使用 {model} 从需求描述生成模型蓝图……")
            config = OpenAIModelDesigner(model=model).design(
                description,
                project_name=args.project_name,
            )
            output = args.output or Path(f"{args.project_name}.yaml")
            save_config_yaml(config, output)
            print(f"模型蓝图已生成：{output.resolve()}（请先 validate 校验后再运行）")
            return

        with args.results.open(encoding="utf-8") as stream:
            result = json.load(stream)
        analyze_results(
            result,
            args.output or args.results.parent,
            model_override=args.model,
            question=args.question,
        )
    except (ValidationError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
