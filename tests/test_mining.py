from __future__ import annotations

from pathlib import Path

from simlab.config import SimulationConfig
from simlab.mining import (
    estimate_model,
    generate_yaml,
    read_log,
    read_xes,
)
from simlab.simulation import run_replication
from simlab.trace import write_jsonl, write_xes

OUTPUT = Path("outputs/smoke")


def events_from_simulation() -> list[dict]:
    simulation = SimulationConfig(
        name="mining-source",
        until=200,
        warmup=0,
        max_arrivals=40,
        arrival_interarrival={"kind": "deterministic", "value": 5},
        stations=[
            # 服务 9 分钟、容量 2：5 分钟的到达间隔必然造成排队，
            # 让并发容量挖掘到 2（若服务快于到达，并发只会是 1）。
            {"name": "desk", "capacity": 2, "service_time": {"kind": "deterministic", "value": 9}},
        ],
    )
    return run_replication(simulation, seed=7, record_traces=True)["trace_events"]


def test_xes_roundtrip_preserves_events(tmp_path: Path) -> None:
    events = events_from_simulation()
    xes_path = write_xes(events, tmp_path / "traces.xes", log_name="roundtrip")
    loaded = read_xes(xes_path)
    assert len(loaded) == len(events)
    first = next(event for event in loaded if event["activity"] == "desk"
                 and event["lifecycle"] == "start")
    assert first["resource"] == "desk"
    assert "wait_time" in first.get("attributes", {})


def _event_key(event: dict) -> tuple[str, float]:
    return (event["case_id"], event["timestamp"])


def test_jsonl_roundtrip(tmp_path: Path) -> None:
    events = events_from_simulation()
    jsonl_path = write_jsonl(events, tmp_path / "traces.jsonl")
    assert sorted(read_log(jsonl_path), key=_event_key) == sorted(events, key=_event_key)


def test_mine_recovers_station_order_and_capacity() -> None:
    events = events_from_simulation()
    config, report = estimate_model(events, project_name="mined")
    assert report["station_order"] == ["desk"]
    assert report["order_coverage"] == 1.0
    assert config["simulation"]["stations"][0]["capacity"] == 2
    assert config["simulation"]["stations"][0]["service_time"]["kind"] == "deterministic"
    assert config["simulation"]["arrival_interarrival"]["kind"] == "deterministic"
    assert abs(config["simulation"]["arrival_interarrival"]["value"] - 5.0) < 0.5
    assert config["simulation"]["until"] <= 200


def test_generate_yaml_writes_valid_config(tmp_path: Path) -> None:
    events = events_from_simulation()
    path, report = generate_yaml(events, tmp_path / "mined.yaml", project_name="mined")
    assert path.exists()
    assert "simulation:" in path.read_text(encoding="utf-8")
    assert report["cases"] > 0


def test_read_log_rejects_unknown_extension(tmp_path: Path) -> None:
    bogus = tmp_path / "traces.csv"
    bogus.write_text("a,b", encoding="utf-8")
    try:
        read_log(bogus)
    except ValueError as error:
        assert "unsupported" in str(error)
    else:  # pragma: no cover - 期望抛出
        raise AssertionError("expected ValueError")


def test_mine_reports_inconsistent_order() -> None:
    events = events_from_simulation()
    # 人为给一部分实体制造不同的工位顺序
    for event in events:
        if event["case_id"].endswith("5") and event["activity"] == "desk":
            event["activity"] = "counter"
    config, report = estimate_model(events, project_name="mined")
    assert report["order_coverage"] < 1.0
    assert report["warnings"], "顺序不一致时应产出警告"
