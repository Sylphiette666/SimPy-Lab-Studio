"""Windows desktop host: private server lifecycle, native window and user data."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import logging
import multiprocessing
import os
import queue
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

APP_NAME = "SimPy Lab Studio"
DESKTOP_VERSION = "1.2.0"
ASSETS = Path(__file__).parent / "static" / "studio"


def user_data_dir() -> Path:
    """Never write user data beside the executable or in its temporary extraction."""
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return base / APP_NAME


def _save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _preferred_port(folder: Path) -> int:
    try:
        port = json.loads((folder / "runtime.json").read_text(encoding="utf-8"))["port"]
        return port if type(port) is int and 1024 <= port <= 65535 else 0
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _server_worker(folder: str, ready, stop) -> None:
    """A spawned child owns the server; closing the desktop can always stop it."""
    import uvicorn

    from simlab.studio import create_studio_app

    root = Path(folder)
    logging.basicConfig(
        filename=root / "server.log",
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            bound.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            bound.bind(("127.0.0.1", _preferred_port(root)))
        except OSError:
            bound.bind(("127.0.0.1", 0))
        bound.listen(128)
        port = bound.getsockname()[1]
        application = create_studio_app(output_root=root / "experiments")
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host="127.0.0.1",
                port=port,
                log_config=None,
                access_log=False,
                loop="asyncio",
                http="h11",
                ws="none",
                timeout_graceful_shutdown=2,
            )
        )

        def supervise():
            while not server.started and not stop.wait(0.05):
                pass
            if server.started:
                ready.put({"port": port})
            stop.wait()
            server.should_exit = True

        threading.Thread(target=supervise, daemon=True).start()
        server.run(sockets=[bound])
    except Exception:
        logging.exception("Desktop server startup failed")
        ready.put({"error": "后台服务启动失败，请查看数据目录中的 server.log。"})
    finally:
        bound.close()


class DesktopServer:
    def __init__(self, folder: Path):
        self.folder = folder.resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        context = multiprocessing.get_context("spawn")
        self.ready = context.Queue()
        self.stop_event = context.Event()
        self.process = context.Process(
            target=_server_worker,
            args=(str(self.folder), self.ready, self.stop_event),
            name="SimPy Lab Studio service",
            daemon=True,
        )
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 45) -> str:
        self.process.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = self.ready.get(timeout=0.15)
            except queue.Empty:
                if not self.process.is_alive():
                    raise RuntimeError("后台服务未能启动，请查看 server.log。") from None
                continue
            if "error" in result:
                raise RuntimeError(result["error"])
            self.port = result["port"]
            _save_json(
                self.folder / "runtime.json",
                {
                    "port": self.port,
                    "pid": os.getpid(),
                    "server_pid": self.process.pid,
                    "running": True,
                    "version": DESKTOP_VERSION,
                },
            )
            return self.url
        raise RuntimeError("后台服务启动超时，请关闭软件后重试。")

    def close(self) -> None:
        self.stop_event.set()
        if self.process.pid is not None:
            self.process.join(timeout=5)
            if self.process.is_alive():
                # Completed versions are already atomic on disk. Interrupted jobs are
                # explicitly marked failed by the session store on the next launch.
                self.process.terminate()
                self.process.join(timeout=3)
        if self.port:
            _save_json(
                self.folder / "runtime.json",
                {
                    "port": self.port,
                    "running": False,
                    "version": DESKTOP_VERSION,
                },
            )
        self.ready.close()


class SingleInstance:
    def __init__(self):
        from ctypes import wintypes

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateMutexW(None, False, r"Local\SimPyLabStudio.Desktop.1")
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == 183

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _message(message: str, *, error: bool = False) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10 if error else 0x40)


def _focus_existing() -> None:
    from ctypes import wintypes

    user = ctypes.WinDLL("user32")
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    for _ in range(20):
        handle = user.FindWindowW(None, APP_NAME)
        if handle:
            user.ShowWindow(handle, 9)
            user.SetForegroundWindow(handle)
            return
        time.sleep(0.1)
    _message("软件已在启动，请稍候。")


def _self_test(folder: Path, report: Path) -> int:
    """Exercise packaged resources and real simulation without opening a UI or API call."""
    import urllib.request

    server = DesktopServer(folder)
    result = {
        "ok": False,
        "version": DESKTOP_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    try:
        url = server.start()

        def request(route, body=None, *, method=None, headers=None):
            payload = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(
                url + route, data=payload,
                headers={"Content-Type": "application/json", **(headers or {})},
                method=method,
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)

        bootstrap = request("/api/studio/bootstrap")
        for asset in ("/", "/static/app.js", "/static/style.css", "/static/app.ico",
                      "/static/adjustments.js", "/static/drafts.js", "/static/visual-editor.js",
                      "/static/durable-storage.js", "/static/productivity.js",
                      "/static/productivity.css"):
            with urllib.request.urlopen(url + asset, timeout=10) as response:
                assert response.status == 200 and response.read()
        config = bootstrap["template"]
        config.update(until_seconds=3600, warmup_seconds=300, replications=2)
        session = request("/api/studio/sessions", {"config": config, "mode": "paper"})
        root = "/api/studio/sessions/" + session["id"]
        run = request(
            root + "/runs", {"kind": "preview", "version_id": session["active_version_id"]}
        )
        deadline = time.monotonic() + 40
        while run["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.15)
            run = request(root + "/runs/" + run["id"])
        assert run["status"] == "succeeded", run.get("error")
        assert run["result"]["frames"]
        checks = ["embedded assets", "server startup", "real SimPy preview"]
        draft = request(root + "/drafts/input", {"value": '{"prompt":"self-test draft"}',
                                                "revision": 0}, method="PUT")
        assert draft["revision"] == 1
        request("/api/studio/workspace", {"last_session_id": session["id"]}, method="PUT")
        batch = request(root + "/batches", {
            "request_id": str(uuid.uuid4()), "expected_version_id": session["active_version_id"],
            "grid": {"machines.0.cycle_time_seconds": [320]}, "targets": {},
        })
        deadline = time.monotonic() + 40
        while batch["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.15)
            batch = request(root + "/batches/" + batch["id"])
        assert batch["status"] == "succeeded"
        backup = request("/api/studio/backups", {}, method="POST")
        with urllib.request.urlopen(url + "/api/studio/backups/" + backup["name"],
                                    timeout=15) as response:
            encoded = base64.b64encode(response.read()).decode()
        restored = request("/api/studio/restore", {"data": encoded, "apply": True})
        restored_root = "/api/studio/sessions/" + restored["session_ids"][0]
        assert request(restored_root + "/drafts")["input"]["value"]
        assert request(restored_root + "/batches")[0]["status"] == "succeeded"
        checks.extend(["durable input draft", "real parameter scan", "backup and isolated restore"])
        if sys.platform == "win32":
            import secrets

            secret = "selftest-" + secrets.token_urlsafe(24)
            profiles_route = "/api/studio/ai/profiles"
            before = {item["id"] for item in request(profiles_route)["profiles"]}
            saved = request(profiles_route, {
                "name": "Self-test credential", "model": "fixture-only",
                "base_url": "https://fixture.invalid/v1", "api_key": secret,
                "remember_key": True,
            })
            profile_id = next(item["id"] for item in saved["profiles"] if item["id"] not in before)
            assert secret not in json.dumps(saved)
            assert secret not in (folder / "experiments/ai_profiles.json").read_text("utf-8")
            server.close()
            server = DesktopServer(folder)
            url = server.start()
            assert request(root + "/drafts")["input"]["value"]
            loaded = next(item for item in request(profiles_route)["profiles"]
                          if item["id"] == profile_id)
            assert loaded["has_key"] and loaded["remember_key"] and not loaded["key_storage_error"]
            recovered = request(profiles_route + "/" + profile_id + "/key",
                                {"base_url": loaded["base_url"]},
                                headers={"X-Simlab-Key-Access": "settings"})
            assert recovered == {"api_key": secret}
            request(profiles_route + "/" + profile_id, method="DELETE")
            assert profile_id not in json.loads(
                (folder / "experiments/ai_profiles.json").read_text("utf-8")
            ).get("protected_keys", {})
            checks.extend([
                "encrypted credential survives server restart", "settings key autofill",
                "saved credential removal",
            ])
        result.update(ok=True, checks=checks)
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        server.close()
        result["server_stopped"] = not server.process.is_alive()
        result["ok"] = result["ok"] and result["server_stopped"]
        _save_json(report, result)
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--self-test", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--debug-port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    folder = (args.data_dir or user_data_dir()).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if args.self_test:
        return _self_test(folder, args.self_test.resolve())
    if sys.platform != "win32":
        raise RuntimeError("此桌面发行版面向 Windows 10/11；其他系统可使用浏览器版。")
    lock = None
    server = None
    logging.basicConfig(
        filename=folder / "desktop.log",
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    try:
        lock = SingleInstance()
        if lock.already_running:
            _focus_existing()
            return 0
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SimPy.Lab.Studio.Desktop")
        import webview
        from webview.menu import Menu, MenuAction

        server = DesktopServer(folder)
        url = server.start()
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["ALLOW_FILE_URLS"] = False
        if args.debug_port and 1024 <= args.debug_port <= 65535:
            webview.settings["REMOTE_DEBUGGING_PORT"] = args.debug_port
        window = webview.create_window(
            APP_NAME,
            url,
            width=1480,
            height=980,
            min_size=(960, 680),
            background_color="#f4f7f5",
            text_select=True,
        )

        def close_auxiliary_windows():
            for other in list(webview.windows):
                if other is not window:
                    other.destroy()

        def show_licenses():
            license_file = ASSETS / "licenses.html"
            if not license_file.is_file():
                _message("源码运行时请先执行构建脚本生成组件许可；EXE 已内置许可信息。")
                return
            webview.create_window(
                "开源组件许可",
                html=license_file.read_text(encoding="utf-8"),
                width=860,
                height=650,
            )

        window.events.closed += close_auxiliary_windows
        instructions = (
            "1. 左侧编辑设备、缓冲区和班次，应用修改后预览。\n"
            "2. 播放、暂停或拖动时间轴，观察加工、堵塞和故障。\n"
            "3. 在“模型接入”保存 API 地址、模型和密钥，右侧随时切换。\n"
            "4. 输入自然语言目标，确认调整方案后生成新版本并重新仿真。\n"
            "5. 执行统计评估比较方案，再导出实验包。\n\n"
            "API 密钥默认仅本次运行有效；可在模型接入中选择本机加密保存。\n"
            "关闭窗口会停止后台服务，未完成的任务下次需重新运行。\n"
            "这是离散事件仿真与二维状态回放，工程使用需用实际数据校准。"
        )
        menu = [
            Menu(
                "文件",
                [
                    MenuAction("打开实验数据目录", lambda: os.startfile(str(folder))),
                    MenuAction("退出", window.destroy),
                ],
            ),
            Menu(
                "帮助",
                [
                    MenuAction("使用说明", lambda: _message(instructions)),
                    MenuAction(
                        "关于软件",
                        lambda: _message(
                            f"{APP_NAME} {DESKTOP_VERSION}\n\n"
                            "制造生产线仿真、可视化回放与 AI 模型辅助调整。\n"
                            "后台服务随软件自动启动，无需 Python 或命令行。\n\n"
                            f"数据目录：\n{folder}\n\n"
                            "开源许可：MIT。组件许可见软件随附的内置许可信息。"
                        ),
                    ),
                    MenuAction("开源组件许可", show_licenses),
                ],
            ),
        ]
        webview.start(
            gui="edgechromium",
            icon=str(ASSETS / "app.ico"),
            private_mode=False,
            storage_path=str(folder / "webview"),
            menu=menu,
            localization={
                "global.saveFile": "保存文件",
                "global.cancel": "取消",
                "global.ok": "确定",
                "windows.fileFilter.allFiles": "所有文件",
            },
        )
        return 0
    except Exception:
        logging.exception("Desktop host failed")
        _message(
            "软件启动失败。请确认系统安装了 Microsoft Edge WebView2 Runtime。\n"
            f"详细日志：{folder / 'desktop.log'}",
            error=True,
        )
        return 1
    finally:
        if server:
            server.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
