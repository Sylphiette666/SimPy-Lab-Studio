"""Integration coverage of the nine high-priority additions; no real provider calls."""

import base64
import copy
import io
import json
import time
import uuid
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError, APITimeoutError

from simlab.manufacturing import case_a_config
from simlab.studio import create_studio_app
from simlab.studio_ai import AISettings
from simlab.studio_extensions import probe
from simlab.studio_statistics import analyze_study, interval, t_critical, t_two_sided_p
from simlab.studio_tables import apply_table, read_table

API = "/api/studio"


@pytest.fixture
def studio(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    app = create_studio_app(
        output_root=tmp_path,
        connection_probe=lambda s: {
            "ok": True,
            "message": s.model,
            "latency_ms": 1,
        },
    )
    with TestClient(app) as client:
        config = case_a_config(until_seconds=1200, warmup_seconds=120, replications=3)
        session = client.post(
            API + "/sessions", json={"config": config.model_dump(), "mode": "custom"}
        ).json()
        yield client, app, session


def b64(data):
    return base64.b64encode(data).decode()


def test_drafts_are_durable_revisioned_and_credential_free(studio, tmp_path):
    client, app, session = studio
    sid = session["id"]
    root = f"{API}/sessions/{sid}/drafts"
    secret = "fixture-secret-not-for-drafts"
    client.put(
        API + "/ai/profiles/default",
        json={"name": "fixture", "model": "fixture", "api_key": secret},
    )
    value = json.dumps({"dirty": True, "prompt": secret, "version": session["active_version_id"]})
    saved = client.put(root + "/input", json={"value": value, "revision": 0})
    assert saved.status_code == 200 and secret not in saved.text
    assert saved.json()["revision"] == 1
    assert client.put(root + "/input", json={"value": "{}", "revision": 0}).status_code == 409
    assert client.put(root + "/unknown", json={"value": "{}", "revision": 0}).status_code == 422
    assert client.put(root + "/graph", json={"value": "bad", "revision": 0}).status_code == 422
    client.put(API + "/workspace", json={"last_session_id": sid, "automatic_backup": True})
    assert client.get(API + "/workspace").json()["drafts"]["input"]["revision"] == 1
    from simlab.studio_workspace import Workspace

    restarted = Workspace(app.state.store, lambda v: v)
    assert restarted.settings()["last_session_id"] == sid
    assert restarted.drafts(sid)["input"]["value"] == saved.json()["value"]
    removed = client.put(root + "/input", json={"value": None, "revision": 1}).json()
    assert removed["revision"] == 2 and removed["value"] is None
    assert secret not in (tmp_path / sid / "drafts.json").read_text()


def wait_job(client, sid, jobid):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        response = client.get(f"{API}/sessions/{sid}/batches/{jobid}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.02)
    raise AssertionError("batch deadline exceeded")


def batch_body(session, **updates):
    return {
        "request_id": str(uuid.uuid4()),
        "expected_version_id": session["active_version_id"],
        "grid": {"machines.0.cycle_time_seconds": [60, 90]},
        "targets": {"throughput_per_hour": 10},
        **updates,
    }


def test_batch_results_idempotency_calibration_and_export(studio):
    client, app, session = studio
    sid = session["id"]
    before = copy.deepcopy(session)
    body = batch_body(session)
    root = f"{API}/sessions/{sid}/batches"
    assert client.post(root, json=body).status_code == 202
    assert client.post(root, json=body).status_code == 202
    altered = {**body, "targets": {}}
    assert client.post(root, json=altered).status_code == 409
    job = wait_job(client, sid, body["request_id"])
    assert job["status"] == "succeeded", job
    assert job["completed_replications"] == job["total_replications"] == 6
    assert len(client.get(root).json()) == 1
    assert all(row["calibration_error"] >= 0 for row in job["scenarios"])
    assert all("replications" not in row for row in job["scenarios"])
    assert "calibration_error" in client.get(root + "/" + job["id"] + "/export").text
    assert client.get(f"{API}/sessions/{sid}").json() == before  # No implicit version changes.
    stored = app.state.extensions.batches.jobs[job["id"]]
    assert (
        stored["scenarios"][0]["replications"][0]["seed"]
        == stored["scenarios"][1]["replications"][0]["seed"]
    )
    assert client.get(f"{API}/sessions/{sid}/parameters").status_code == 200
    workspace = app.state.extensions.workspace
    restored = workspace.restore(workspace.inspect_archive(workspace.archive_bytes([sid])))
    restored_id = restored["session_ids"][0]
    restored_jobs = client.get(f"{API}/sessions/{restored_id}/batches").json()
    assert len(restored_jobs) == 1 and restored_jobs[0]["status"] == "succeeded"
    assert restored_jobs[0]["id"] != job["id"]
    for grid in [
        {"bad": [1]},
        {"buffers.0.capacity": [1.5]},
        {"machines.0.availability": [-1]},
        {"machines.0.cycle_time_seconds": list(range(1, 17)), "buffers.0.capacity": [1, 2]},
    ]:
        assert client.post(root, json=batch_body(session, grid=grid)).status_code == 422
    assert client.post(root, json=batch_body(session, targets={"avg_wip": -1})).status_code == 422
    assert client.post(root, json=batch_body(session, request_id="bad")).status_code == 422
    assert (
        client.post(root, json=batch_body(session, grid={"buffers.0.capacity": [1, 1]})).status_code
        == 422
    )
    assert client.get(root + "/missing").status_code == 404


def test_batch_cancel_interrupt_failure_and_queue_bound(studio, monkeypatch):
    import threading

    import simlab.studio_batches as module

    client, app, session = studio
    started, release = threading.Event(), threading.Event()
    original = module.run_manufacturing_replication

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "run_manufacturing_replication", slow)
    root = f"{API}/sessions/{session['id']}/batches"
    first, second = batch_body(session), batch_body(session)
    try:
        client.post(root, json=first)
        assert started.wait(2)
        client.post(root, json=second)
        assert client.post(root, json=batch_body(session)).status_code == 429
        client.post(root + "/" + first["request_id"] + "/cancel")
        client.post(root + "/" + second["request_id"] + "/cancel")
    finally:
        release.set()
    assert wait_job(client, session["id"], first["request_id"])["status"] == "cancelled"
    assert wait_job(client, session["id"], second["request_id"])["status"] == "cancelled"
    monkeypatch.setattr(module, "run_manufacturing_replication", lambda *a, **k: 1 / 0)
    failed = batch_body(session)
    client.post(root, json=failed)
    assert wait_job(client, session["id"], failed["request_id"])["status"] == "failed"
    saved = app.state.extensions.batches.jobs[first["request_id"]]
    saved["status"] = "running"
    app.state.extensions.batches.save(saved)
    restarted = module.Batches(app.state.store, app.state.extensions.batches.validate)
    assert restarted.jobs[saved["id"]]["status"] == "interrupted"
    restarted.close()


def test_table_csv_preview_mapping_errors_and_no_mutation(studio):
    client, _, session = studio
    config = session["versions"][0]["config"]
    downloaded = client.post(API + "/tables/template", json=config)
    table = read_table(downloaded.content, "parameters.csv")
    assert apply_table(table, config, table["mapping"]) == config
    data = "对象类型,序号,加工时间(s),可用率(%),平均维修时间(s)\n设备,1,80,90,60\n".encode()
    payload = {"filename": "test.csv", "data": b64(data)}
    info = client.post(API + "/tables/preview", json=payload).json()
    preview = client.post(
        API + "/tables/preview", json={**payload, "config": config, "mapping": info["mapping"]}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["config"]["machines"][0]["availability"] == 0.9
    assert client.get(f"{API}/sessions/{session['id']}").json() == session
    invalid = {
        **payload,
        "data": b64("对象类型,序号,加工时间(s)\n设备,99,abc\n设备,1,nan".encode()),
        "config": config,
        "mapping": info["mapping"],
    }
    result = client.post(API + "/tables/preview", json=invalid)
    assert result.status_code == 422 and "rows" in result.json()["detail"]
    assert client.post(API + "/tables/preview", json={**payload, "data": "!bad"}).status_code == 422
    for raw in [b"a,a\n1,2", b"a\n", b"a\n1,2", b"", b"a,b\n" + b"1,2\n" * 501]:
        with pytest.raises(HTTPException):
            read_table(raw, "test.csv")
    for filename in ("test.xls", "test.xlsx"):
        with pytest.raises(HTTPException):
            read_table(b"invalid", filename)
    with pytest.raises(HTTPException):
        read_table(b"x" * 1000001, "test.csv")
    with pytest.raises(HTTPException):
        apply_table(info, config, {})


def workbook(sheet, *, shared="", sheets=1):
    data = io.BytesIO()
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + '<sheet name="参数" sheetId="1" r:id="rId1"/>' * sheets
            + "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="rId1" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {ns}><sheetData>{sheet}</sheetData></worksheet>",
        )
        if shared:
            archive.writestr("xl/sharedStrings.xml", f"<sst {ns}>{shared}</sst>")
    return data.getvalue()


def test_xlsx_shared_inline_sparse_and_formula_guard():
    data = workbook(
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr">'
        '<is><t>序号</t></is></c></row><row r="2"><c r="A2" t="inlineStr">'
        '<is><t>设备</t></is></c><c r="B2"><v>1</v></c></row>',
        shared="<si><t>对象类型</t></si>",
    )
    result = read_table(data, "test.xlsx")
    assert result["rows"] == [["设备", "1"]]
    assert result["mapping"] == {"对象类型": "type", "序号": "index"}
    with pytest.raises(HTTPException, match="公式"):
        read_table(workbook('<row><c r="A1"><f>1+1</f><v>2</v></c></row>'), "test.xlsx")
    with pytest.raises(HTTPException):
        read_table(workbook("", sheets=2), "test.xlsx")


@pytest.mark.parametrize(
    "df,expected", [(1, 12.706204736), (2, 4.302652730), (9, 2.262157163), (49, 2.009575237)]
)
def test_t_distribution_against_published_critical_values(df, expected):
    assert t_critical(0.95, df) == pytest.approx(expected, abs=1e-8)
    assert t_two_sided_p(expected, df) == pytest.approx(0.05, abs=1e-8)


def test_statistics_pairing_missing_zero_variance_and_precision():
    config = case_a_config(replications=4).model_dump()
    records = [
        {"replication": i, "seed": i + 10, "metrics": {"throughput_per_hour": v}}
        for i, v in enumerate([10, 12, 11, 9])
    ]
    study = {"config": config, "replications": records}
    control = copy.deepcopy(study)
    for i, row in enumerate(control["replications"]):
        row["metrics"]["throughput_per_hour"] -= 2 + i * 0.1
    result = analyze_study(study, control)
    assert result["metrics"][0]["paired"]["conclusion"] == "改善"
    assert result["metrics"][1]["n"] == 0
    assert result["metrics"][1]["paired"]["n"] == 0
    assert analyze_study(study, study)["metrics"][0]["paired"]["conclusion"] == "证据不足"
    control["config"]["base_seed"] += 1
    assert not analyze_study(study, control)["comparable"]
    assert interval([1])["ci_low"] is None
    assert interval([None, float("nan"), True])["n"] == 0
    with pytest.raises(ValueError):
        t_critical(1, 2)
    with pytest.raises(ValueError):
        t_two_sided_p(1, 0)


def test_metadata_backup_restore_isolated_and_legacy_exports(studio, tmp_path):
    client, app, session = studio
    sid = session["id"]
    root = f"{API}/sessions/{sid}"
    metadata = {"display_name": "生产线甲", "tags": ["产能", "验证"], "archived": True}
    assert client.put(root + "/metadata", json=metadata).status_code == 200
    assert client.get(API + "/sessions").json()["sessions"][0]["name"] == "生产线甲"
    client.put(root + "/drafts/input", json={"value": '{"prompt":"未发送"}', "revision": 0})
    run = client.post(
        root + "/runs", json={"version_id": session["active_version_id"], "kind": "study"}
    ).json()
    for _ in range(200):
        result = client.get(root + "/runs/" + run["id"]).json()
        if result["status"] == "succeeded":
            break
        time.sleep(0.01)
    assert result["status"] == "succeeded"
    analysis = client.get(root + "/analysis/" + session["active_version_id"])
    assert analysis.status_code == 200 and len(analysis.json()["machines"]) == 4
    assert (
        client.get(root + "/analysis/" + session["active_version_id"] + "?precision=0").status_code
        == 422
    )
    backup = client.post(API + "/backups").json()
    raw = client.get(API + "/backups/" + backup["name"]).content
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert all("ai_profiles" not in name for name in archive.namelist())
    preview = client.post(API + "/restore", json={"data": b64(raw)}).json()
    assert preview["sessions"][0]["name"] == "生产线甲（恢复）"
    restored = client.post(API + "/restore", json={"data": b64(raw), "apply": True})
    assert restored.status_code == 200, restored.text
    new_id = restored.json()["session_ids"][0]
    assert new_id != sid
    assert (
        client.get(f"{API}/sessions/{new_id}/runs/{run['id']}").json()["result"] == result["result"]
    )
    restored_draft = client.get(f"{API}/sessions/{new_id}/drafts").json()["input"]["value"]
    assert json.loads(restored_draft)["prompt"] == "未发送"
    legacy = client.get(root + "/export").content
    assert (
        client.post(API + "/restore", json={"data": b64(legacy), "apply": True}).status_code == 200
    )
    assert client.get(API + "/backups/missing.zip").status_code == 404
    assert client.post(API + "/restore", json={"data": b64(b"bad")}).status_code == 422
    for _ in range(8):
        app.state.extensions.workspace.backup()
    assert len(list((tmp_path / "backups").glob("backup-*.zip"))) == 7
    assert len(client.get(API + "/backups").json()["items"]) == 7


def test_restore_rejects_path_traversal_and_rolls_back(studio, monkeypatch, tmp_path):
    client, app, session = studio
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../outside", "bad")
        archive.writestr("session.json", json.dumps(session))
    assert client.post(API + "/restore", json={"data": b64(buffer.getvalue())}).status_code == 422
    workspace = app.state.extensions.workspace
    prepared = workspace.inspect_archive(workspace.archive_bytes())
    original = Path.rename

    def broken(path, target):
        if "restore-" in str(path):
            raise OSError("fixture")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", broken)
    with pytest.raises(OSError):
        workspace.restore(prepared)
    assert set(app.state.store.sessions) == {session["id"]}
    assert not list(tmp_path.glob("restore-*"))


def test_connection_test_uses_unsaved_snapshot_and_endpoint_isolation(studio):
    client, _, _ = studio
    body = {
        "name": "test",
        "model": "old",
        "base_url": "https://one.invalid/v1",
        "api_key": "fixture-key",
    }
    client.put(API + "/ai/profiles/default", json=body)
    test = {
        "name": "unsaved",
        "model": "new",
        "base_url": body["base_url"],
        "profile_id": "default",
    }
    assert client.post(API + "/ai/test", json=test).json()["message"] == "new"
    assert client.get(API + "/ai/profiles").json()["profiles"][0]["model"] == "old"
    assert (
        client.post(
            API + "/ai/test", json={**test, "base_url": "https://other.invalid"}
        ).status_code
        == 422
    )
    assert client.post(API + "/ai/test", json={**test, "api_key": ""}).status_code == 422


@pytest.mark.parametrize(
    "kind,category",
    [
        (401, "authentication"),
        (403, "permission"),
        (404, "not_found"),
        (429, "rate_limit"),
        (500, "protocol"),
        ("timeout", "timeout"),
        ("network", "connection"),
        ("unexpected", "unexpected"),
    ],
)
def test_connection_errors_sanitized(monkeypatch, kind, category):
    request = httpx.Request("POST", "https://fixture.invalid")
    if isinstance(kind, int):
        error = APIStatusError(
            "private-provider-body", response=httpx.Response(kind, request=request), body=None
        )
    elif kind == "timeout":
        error = APITimeoutError(request=request)
    elif kind == "network":
        error = APIConnectionError(request=request)
    else:
        error = RuntimeError("private-provider-body")

    def fail(**kwargs):
        raise error

    monkeypatch.setattr("simlab.studio_extensions.OpenAI", fail)
    result = probe(AISettings(api_key="fixture-key"))
    assert result["category"] == category and "private-provider-body" not in str(result)


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_probe_real_sdk_payload_without_network(monkeypatch, protocol):
    from openai import OpenAI

    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        data = {
            "id": "fixture",
            "object": "chat.completion",
            "created": 0,
            "model": "fixture",
            "choices": [],
            "usage": {"total_tokens": 2},
        }
        if protocol == "responses":
            data = {
                "id": "fixture",
                "object": "response",
                "created_at": 0,
                "model": "fixture",
                "output": [],
                "status": "completed",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        return httpx.Response(200, json=data)

    def factory(**kwargs):
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    monkeypatch.setattr("simlab.studio_extensions.OpenAI", factory)
    result = probe(
        AISettings(
            model="fixture",
            api_key="fixture-only",
            base_url="https://api.deepseek.com/v1",
            api_format=protocol,
        )
    )
    assert result["ok"] and result["total_tokens"] == 2
    assert captured[0]["model"] == "fixture"
    if protocol == "chat_completions":
        assert captured[0]["thinking"] == {"type": "disabled"}
    else:
        assert captured[0]["store"] is False


def test_automatic_backup_due_and_failure_keep_existing_data(studio, monkeypatch):
    client, app, _ = studio
    workspace = app.state.extensions.workspace
    workspace.configure({"automatic_backup": True, "last_backup_at": 0})
    checks = iter([False, True])
    monkeypatch.setattr(workspace.stopped, "wait", lambda _: next(checks))
    workspace._loop()
    assert workspace.backups() and workspace.data["last_backup_at"] > 0
    workspace.data["last_backup_at"] = 0
    checks = iter([False, True])
    monkeypatch.setattr(workspace, "backup", lambda: (_ for _ in ()).throw(OSError("fixture")))
    workspace._loop()
    assert "自动备份未完成" in client.get(API + "/workspace").json()["backup_error"]
