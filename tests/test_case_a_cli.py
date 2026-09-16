from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from simlab.cli import build_parser, main


def test_case_a_defaults_match_paper_run() -> None:
    args = build_parser().parse_args(["case-a"])

    assert (args.days, args.warmup_days, args.replications) == (30, 1, 25)
    assert args.seed is None  # Keep the reproducible seed from case_a_config().
    assert args.workers == 1
    assert args.output == Path("outputs/case_a")
    assert args.include_infeasible_v1 is False


def test_case_a_cli_exports_baseline_and_feasible_variants(tmp_path, capsys) -> None:
    output = tmp_path / "case-a-results"
    main([
        "case-a", "--horizon-days", "0.1", "--warmup-days", "0.01",
        "--reps", "2", "--seed", "13", "--output", str(output),
    ])

    result = json.loads((output / "results.json").read_text(encoding="utf-8"))
    scenarios = {scenario["name"]: scenario for scenario in result["scenarios"]}
    assert set(scenarios) == {"baseline", "V2", "V3"}
    assert all(item["feasible"] and not item["violations"] for item in scenarios.values())
    assert len(result["replications"]) == 6
    assert {row["scenario"] for row in result["replications"]} == set(scenarios)
    assert {row["scenario"] for row in result["summary"]} == set(scenarios)
    assert result["config"]["until_seconds"] == 8640
    assert result["config"]["warmup_seconds"] == 864
    assert result["config"]["base_seed"] == 13
    assert result["assumptions"]
    for name in ("summary.csv", "replications.csv"):
        with (output / name).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert rows
        assert {row["scenario"] for row in rows} == set(scenarios)
    report = (output / "comparison.md").read_text(encoding="utf-8")
    assert "baseline" in report and "V2" in report and "V3" in report
    assert str(output.resolve()) in capsys.readouterr().out


def test_case_a_cli_infeasible_v1_requires_flag_and_stays_labelled(tmp_path, capsys) -> None:
    main([
        "case-a", "--days", "0.1", "--warmup-days", "0.01",
        "--replications", "1", "--include-infeasible-v1", "--output", str(tmp_path),
    ])

    result = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    scenarios = {scenario["name"]: scenario for scenario in result["scenarios"]}
    assert set(scenarios) == {"baseline", "V1", "V2", "V3"}
    assert scenarios["V1"]["feasible"] is False
    assert scenarios["V1"]["violations"]
    assert len(result["replications"]) == 4
    assert "不属于可接受的改进方案" in capsys.readouterr().out


@pytest.mark.parametrize(
    "options",
    [
        ["--days", "0"],
        ["--days", "-1"],
        ["--days", "nan"],
        ["--days", "inf"],
        ["--days", "1", "--warmup-days", "1"],
        ["--warmup-days", "-0.1"],
        ["--warmup-days", "nan"],
        ["--replications", "0"],
        ["--workers", "0"],
        ["--seed", "-1"],
    ],
)
def test_case_a_cli_rejects_invalid_settings_before_writing(tmp_path, capsys, options) -> None:
    output = tmp_path / "invalid-result"
    with pytest.raises(SystemExit) as error:
        main(["case-a", *options, "--output", str(output)])

    assert error.value.code == 2
    assert "错误：" in capsys.readouterr().err
    assert not output.exists()


def test_existing_validate_command_remains_usable(tmp_path, capsys) -> None:
    config = tmp_path / "service.yaml"
    config.write_text(
        "simulation:\n"
        "  until: 20\n"
        "  arrival_interarrival: {kind: deterministic, value: 2}\n"
        "  stations:\n"
        "    - name: server\n"
        "      capacity: 1\n"
        "      service_time: {kind: deterministic, value: 1}\n"
        "experiment:\n"
        "  replications: 2\n",
        encoding="utf-8",
    )

    main(["validate", str(config)])

    assert "配置有效：1 个场景，每场景 2 次，共 2 次仿真。" in capsys.readouterr().out
