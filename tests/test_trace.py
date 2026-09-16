from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from simlab.config import SimulationConfig
from simlab.simulation import run_replication
from simlab.trace import TraceRecorder, write_jsonl, write_xes

NS = {"xes": "http://www.xes-standard.org/"}


def simulation_config() -> SimulationConfig:
    return SimulationConfig(
        name="trace-test",
        until=30,
        warmup=0,
        max_arrivals=2,
        arrival_interarrival={"kind": "deterministic", "value": 5},
        stations=[
            {
                "name": "desk",
                "capacity": 1,
                "service_time": {"kind": "deterministic", "value": 3},
            }
        ],
    )


def test_recorder_disabled_records_nothing() -> None:
    recorder = TraceRecorder(enabled=False)
    recorder.arrival(1, 0.0)
    recorder.queue_enter(1, "desk", 0.0)
    recorder.service_start(1, "desk", 0.0)
    recorder.service_end(1, "desk", 3.0, 3.0)
    recorder.completion(1, 3.0)
    assert recorder.events == []


def test_recorder_captures_ordered_trace_with_wait_attributes() -> None:
    recorder = TraceRecorder(enabled=True, case_prefix="r0:")
    recorder.arrival(7, 0.0)
    recorder.queue_enter(7, "desk", 0.0)
    recorder.service_start(7, "desk", 2.5)
    recorder.service_end(7, "desk", 5.5, 3.0)
    recorder.completion(7, 5.5)

    assert [event["activity"] for event in recorder.events] == [
        "arrival",
        "desk",
        "desk",
        "completion",
    ]
    start = recorder.events[1]
    assert start["lifecycle"] == "start"
    assert start["resource"] == "desk"
    assert start["attributes"]["wait_time"] == 2.5
    complete = recorder.events[2]
    assert complete["lifecycle"] == "complete"
    assert complete["attributes"]["service_time"] == 3.0


def test_run_replication_returns_trace_events_when_enabled() -> None:
    result = run_replication(simulation_config(), seed=1, record_traces=True)
    events = result["trace_events"]
    assert events, "record_traces=True 应产生轨迹事件"
    assert any(event["activity"] == "arrival" for event in events)
    assert any(event["activity"] == "desk" and event["lifecycle"] == "complete" for event in events)
    assert any(event["activity"] == "completion" for event in events)


def test_run_replication_trace_events_empty_by_default() -> None:
    result = run_replication(simulation_config(), seed=1)
    assert result["trace_events"] == []


def test_xes_writer_produces_well_formed_log() -> None:
    recorder = TraceRecorder(enabled=True, case_prefix="r0:")
    recorder.arrival(1, 0.0)
    recorder.queue_enter(1, "desk", 0.0)
    recorder.service_start(1, "desk", 1.0)
    recorder.service_end(1, "desk", 4.0, 3.0)
    recorder.completion(1, 4.0)
    path = write_xes(recorder.events, "outputs/smoke/test_traces.xes", log_name="unit-test")

    tree = ET.parse(path)
    root = tree.getroot()
    assert root.tag == f"{{{NS['xes']}}}log"
    traces = root.findall("xes:trace", NS)
    assert len(traces) == 1
    events = traces[0].findall("xes:event", NS)
    assert len(events) == 4
    keys = {element.get("key") for element in events[2].findall("xes:*", NS)}
    assert "time:timestamp" in keys
    assert "org:resource" in keys
    assert "simlab:service_time" in keys


def test_jsonl_writer_round_trips_events() -> None:
    recorder = TraceRecorder(enabled=True, case_prefix="r0:")
    recorder.arrival(2, 0.0)
    recorder.queue_enter(2, "desk", 0.0)
    recorder.service_start(2, "desk", 0.5)
    recorder.service_end(2, "desk", 3.5, 3.0)
    recorder.completion(2, 3.5)
    path = write_jsonl(recorder.events, "outputs/smoke/test_traces.jsonl")

    loaded = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(loaded) == 4
    assert loaded[0]["activity"] == "arrival"
    assert loaded[-1]["activity"] == "completion"
    assert loaded[1]["attributes"]["wait_time"] == 0.5
