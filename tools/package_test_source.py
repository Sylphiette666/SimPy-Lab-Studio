"""Package the rolling working sources with hashes, excluding user data and binaries."""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {"src", "tests", "tools", "docs", "examples", "installer", ".github"}
ROOT_FILES = {".gitignore", ".gitattributes", "AGENTS.md", "README.md", "LICENSE",
              "pyproject.toml", "desktop_entry.py", "desktop.spec", "start_studio.cmd", "优化.md"}


def main():
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).decode("utf-8").split("\0")
    files = []
    for name in sorted(set(filter(None, names))):
        path = Path(name)
        if path.parts[0] not in SOURCE_DIRS and name not in ROOT_FILES:
            continue
        if path.suffix.lower() in {".exe", ".zip", ".pyc"} or ".env" in path.name:
            continue
        if (ROOT / path).is_file():
            files.append(name)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    manifest = {"base_commit": base, "snapshot": "working-tree", "files": {}}
    target = ROOT / "source-packages" / "SimPy-Lab-Studio-test-source.zip"
    temporary = target.with_suffix(".zip.tmp")
    prefix = target.stem + "/"
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            data = (ROOT / name).read_bytes()
            archive.writestr(prefix + name, data)
            manifest["files"][name] = hashlib.sha256(data).hexdigest()
        archive.writestr(prefix + "SOURCE_MANIFEST.json",
                         json.dumps(manifest, indent=2, ensure_ascii=False))
        archive.writestr(prefix + "SOURCE_GUIDE.zh-CN.md", (
            "# 滚动测试版源码\n\n本包包含未正式发布的可靠性及模型接入窗口更新，请先阅读 "
            "docs/reliability-update.md 和 docs/model-settings-update.md。"
            "来源为 manifest 中基线提交加当前工作区修改。\n\n"
            "使用 Python 3.11+：`python -m pip install -e \".[desktop,dev]\"`，"
            "然后 `python desktop_entry.py --data-dir outputs/test-data`。\n"
            "不包含用户数据、密钥、运行环境或 EXE。SOURCE_MANIFEST.json 可逐文件校验。\n"
        ))
    with zipfile.ZipFile(temporary) as archive:
        assert archive.testzip() is None
        for name, expected in manifest["files"].items():
            assert hashlib.sha256(archive.read(prefix + name)).hexdigest() == expected
    temporary.replace(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    print(json.dumps({"path": str(target), "files": len(files), "sha256": digest}))


if __name__ == "__main__":
    main()
