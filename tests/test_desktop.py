import json
import socket
import urllib.request

import pytest

from simlab.desktop import DesktopServer, user_data_dir


def test_user_data_is_outside_executable_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert user_data_dir() == tmp_path / "SimPy Lab Studio"


def test_owned_server_restarts_on_same_port_and_preserves_data(tmp_path):
    def fetch(server, route, body=None):
        request = urllib.request.Request(
            server.url + "/api/studio" + route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    first = DesktopServer(tmp_path)
    try:
        first.start()
        config = fetch(first, "/bootstrap")["template"]
        created = fetch(first, "/sessions", {"config": config})
        saved_port = first.port
    finally:
        first.close()
    assert not first.process.is_alive()
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", saved_port)) != 0
    second = DesktopServer(tmp_path)
    try:
        second.start()
        assert second.port == saved_port
        assert fetch(second, "/sessions/" + created["id"])["id"] == created["id"]
    finally:
        second.close()
    assert json.loads((tmp_path / "runtime.json").read_text())["running"] is False


def test_busy_port_falls_back_without_using_existing_service(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        busy_port = occupied.getsockname()[1]
        (tmp_path / "runtime.json").write_text(json.dumps({"port": busy_port}))
        server = DesktopServer(tmp_path)
        try:
            server.start()
            assert server.port != busy_port
            assert server.url.startswith("http://127.0.0.1:")
        finally:
            server.close()
        assert not server.process.is_alive()


def test_start_failure_can_be_closed_without_leaving_a_worker(tmp_path):
    # A file where the experiment directory should be forces worker startup to fail.
    (tmp_path / "experiments").write_text("occupied")
    server = DesktopServer(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            server.start(timeout=15)
    finally:
        server.close()
    assert not server.process.is_alive()
