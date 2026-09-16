from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from simlab.cli import build_parser, main
from simlab.studio_cli import run_studio


@pytest.fixture
def launch_dependencies(monkeypatch):
    """Isolate startup from optional packages, sockets, timers and the user's browser."""
    application = object()
    factory = Mock(return_value=application)
    server = Mock()
    browser = Mock()
    timer = Mock()
    timer_factory = Mock(return_value=timer)
    studio_module = ModuleType("simlab.studio")
    studio_module.create_studio_app = factory
    uvicorn_module = ModuleType("uvicorn")
    uvicorn_module.run = server
    monkeypatch.setitem(sys.modules, "simlab.studio", studio_module)
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn_module)
    monkeypatch.setattr("simlab.studio_cli.webbrowser.open", browser)
    monkeypatch.setattr("simlab.studio_cli.threading.Timer", timer_factory)
    return application, factory, server, browser, timer, timer_factory


def test_studio_parser_defaults_and_explicit_options() -> None:
    args = build_parser().parse_args(["studio"])
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.output == Path("outputs/studio")
    assert args.open_browser is False
    custom = build_parser().parse_args(
        ["studio", "--host", "::1", "--port", "8766", "--output", "saved", "--open-browser"]
    )
    assert (custom.host, custom.port, custom.output, custom.open_browser) == (
        "::1",
        8766,
        Path("saved"),
        True,
    )


def test_launcher_passes_created_application_and_output_to_server(
    tmp_path, capsys, launch_dependencies
) -> None:
    application, factory, server, browser, timer, timer_factory = launch_dependencies
    output = tmp_path / "experiment sessions"
    run_studio(host="localhost", port=8766, output_root=output)
    factory.assert_called_once_with(output_root=output)
    server.assert_called_once_with(application, host="localhost", port=8766, log_level="info")
    browser.assert_not_called()
    timer_factory.assert_not_called()
    timer.start.assert_not_called()
    printed = capsys.readouterr().out
    assert "http://localhost:8766" in printed
    assert str(output.resolve()) in printed


@pytest.mark.parametrize(
    ("host", "expected_url"),
    [("127.0.0.1", "http://127.0.0.1:8765"), ("::1", "http://[::1]:8765")],
)
def test_browser_open_is_optional_delayed_daemon_with_correct_local_url(
    host, expected_url, launch_dependencies
) -> None:
    _, _, _, browser, timer, timer_factory = launch_dependencies
    run_studio(host=host, open_browser=True)
    timer_factory.assert_called_once()
    timer_args, timer_kwargs = timer_factory.call_args
    assert timer_args[0] > 0
    assert timer_args[1] is browser
    assert timer_kwargs == {"args": (expected_url,)}
    assert timer.daemon is True
    timer.start.assert_called_once_with()
    browser.assert_not_called()  # The mocked timer never opens a real browser.


@pytest.mark.parametrize(
    "kwargs", [{"host": "0.0.0.0"}, {"host": "192.168.1.2"}, {"port": 0}, {"port": 65536}]
)
def test_launcher_rejects_invalid_bind_options_before_creating_app(
    kwargs, launch_dependencies
) -> None:
    _, factory, server, _, _, timer_factory = launch_dependencies
    with pytest.raises(ValueError):
        run_studio(**kwargs)
    factory.assert_not_called()
    server.assert_not_called()
    timer_factory.assert_not_called()


@pytest.mark.parametrize("missing_module", ["uvicorn", "simlab.studio"])
def test_missing_optional_dependency_has_actionable_install_message(
    monkeypatch, missing_module, launch_dependencies
) -> None:
    real_import = builtins.__import__

    def import_without_studio_dependency(name, *args, **kwargs):
        if name == missing_module:
            raise ImportError(f"Missing {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_studio_dependency)
    with pytest.raises(RuntimeError, match="Studio 依赖缺失") as error:
        run_studio()
    assert 'python -m pip install -e ".[studio]"' in str(error.value)
    assert isinstance(error.value.__cause__, ImportError)
    launch_dependencies[2].assert_not_called()


def test_cli_forwards_options_to_studio_launcher(monkeypatch, tmp_path) -> None:
    launcher = Mock()
    monkeypatch.setattr("simlab.studio_cli.run_studio", launcher)
    main(
        [
            "studio",
            "--host",
            "localhost",
            "--port",
            "9000",
            "--output",
            str(tmp_path),
            "--open-browser",
        ]
    )
    launcher.assert_called_once_with(
        host="localhost",
        port=9000,
        output_root=tmp_path,
        open_browser=True,
    )


@pytest.mark.parametrize("options", [["--host", "0.0.0.0"], ["--port", "0"], ["--port", "65536"]])
def test_cli_reports_bind_errors_without_running_server(
    options, capsys, launch_dependencies
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["studio", *options])
    assert error.value.code == 2
    assert "错误：" in capsys.readouterr().err
    launch_dependencies[2].assert_not_called()


def test_cli_reports_missing_dependencies_without_traceback(monkeypatch, capsys) -> None:
    launcher = Mock(
        side_effect=RuntimeError('Studio 依赖缺失；python -m pip install -e ".[studio]"')
    )
    monkeypatch.setattr("simlab.studio_cli.run_studio", launcher)
    with pytest.raises(SystemExit) as error:
        main(["studio"])
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert ".[studio]" in stderr
    assert "Traceback" not in stderr


def test_cli_rejects_non_integer_port_before_launcher(monkeypatch, capsys) -> None:
    launcher = Mock()
    monkeypatch.setattr("simlab.studio_cli.run_studio", launcher)
    with pytest.raises(SystemExit) as error:
        main(["studio", "--port", "8765.5"])
    assert error.value.code == 2
    assert "invalid int value" in capsys.readouterr().err
    launcher.assert_not_called()
