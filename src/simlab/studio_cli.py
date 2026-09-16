"""Launch the local manufacturing studio without exposing it to the network."""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path


def run_studio(
    *, host: str = "127.0.0.1", port: int = 8765,
    output_root: str | Path = "outputs/studio", open_browser: bool = False,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Studio 当前为本机应用，--host 请使用 127.0.0.1、localhost 或 ::1")
    if not 1 <= port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    try:
        import uvicorn

        from simlab.studio import create_studio_app
    except ImportError as error:
        raise RuntimeError(
            'Studio 依赖缺失；请先运行：python -m pip install -e ".[studio]"'
        ) from error

    application = create_studio_app(output_root=output_root)
    display_host = f"[{host}]" if ":" in host else host
    url = f"http://{display_host}:{port}"
    print(f"SimLab Studio：{url}", flush=True)
    print(f"会话保存目录：{Path(output_root).resolve()}", flush=True)
    if open_browser:
        opener = threading.Timer(1.2, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    uvicorn.run(application, host=host, port=port, log_level="info")
