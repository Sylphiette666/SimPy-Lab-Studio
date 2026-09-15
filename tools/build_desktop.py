"""Create the Windows single-file desktop application and its embedded notices."""

from __future__ import annotations

import html
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "simlab" / "static" / "studio"


def licenses():
    sections = []
    vendor_manifest = ASSETS / "vendor" / "manifest.json"
    if vendor_manifest.is_file():
        for name, package in json.loads(vendor_manifest.read_text(encoding="utf-8")).items():
            notice = (ASSETS / "vendor" / name / "LICENSE").read_text(encoding="utf-8")
            sections.append(
                f"<details><summary>{html.escape(name)} {html.escape(package['version'])}"
                f" (offline conversation renderer)</summary><pre>{html.escape(notice)}"
                "</pre></details>"
            )
    # Include notices from all distributions in the build environment, including
    # transitive native components' package license files.
    for distribution in sorted(
        importlib.metadata.distributions(), key=lambda d: d.metadata.get("Name", "").lower()
    ):
        name = distribution.metadata.get("Name", "Unknown")
        blocks = []
        for file in distribution.files or []:
            if any(
                part.lower().startswith(("license", "copying", "notice", "copyright"))
                for part in file.parts
            ):
                path = Path(distribution.locate_file(file))
                if path.is_file() and path.stat().st_size < 600_000:
                    try:
                        blocks.append(path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError):
                        pass
        fallback = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or "See upstream package."
        )
        sections.append(
            f"<details><summary>{html.escape(name)} {distribution.version}</summary>"
            f"<pre>{html.escape(chr(10).join(blocks) or fallback)}</pre></details>"
        )
    sections.insert(
        0,
        "<h2>SimPy Lab Studio — MIT</h2><pre>"
        + html.escape((ROOT / "LICENSE").read_text(encoding="utf-8"))
        + "</pre>",
    )
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        sections.append(
            "<h2>Python</h2><pre>"
            + html.escape(python_license.read_text(encoding="utf-8"))
            + "</pre>"
        )
    (ASSETS / "licenses.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        "<title>开源组件许可</title><style>body{font:16px system-ui;padding:24px;"
        "color:#183e37;line-height:1.6}pre{white-space:pre-wrap;overflow-wrap:anywhere;"
        "font-size:12px}summary{cursor:pointer;padding:8px}</style>"
        "<h1>开源组件许可</h1><p>本软件内置 Python 与开源组件。以下为构建环境提供的许可文本；"
        "部分开发工具不在成品中。WebView2 Runtime 由 Microsoft 单独提供。</p>"
        + "".join(sections)
        + "</html>",
        encoding="utf-8",
    )


def main():
    if sys.platform != "win32":
        raise SystemExit("Build the Windows executable on Windows.")
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_desktop_icon.py")], check=True)
    licenses()
    build = ROOT / "build" / "desktop"
    build.mkdir(parents=True, exist_ok=True)
    manifest = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(ROOT / "desktop.spec")],
        cwd=ROOT,
        check=True,
    )
    (build / "build-environment.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
