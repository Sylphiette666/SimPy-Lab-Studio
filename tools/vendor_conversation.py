"""Fetch pinned offline conversation renderers, checking npm archive integrity."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/simlab/static/studio/vendor"
PACKAGES = {
    "marked": (
        "18.0.13",
        "xTxVzZsBFwunP6HDmtBkabUQEYArnP7/rMDGmPj9SlrKlQ4i8MdYVow+nJL0eOqwpUqhzBoTBRADGN6uYwPyOw==",
        ["lib/marked.umd.js", "LICENSE"],
    ),
    "dompurify": (
        "3.4.15",
        "EUBjM+B+lkDE41iE82DDSCfkoPGfXx8IxFxPMjNzm/Uk4xDet77rTN9wqlxlVg71kK7XGuUMv6wUxJUwwv+Xyw==",
        ["dist/purify.min.js", "LICENSE"],
    ),
    "katex": (
        "0.18.7",
        "h+UCwkZ+4Jz8WQ7MLGfj7UVFrRCizGb912fwF4luGdYsC5paYG1vx+jy+KRcC/XkpjGva/P7nAWuxNnPzRvzHw==",
        ["dist/katex.min.js", "dist/katex.min.css", "LICENSE"],
    ),
}


def main():
    manifest = {}
    for name, (version, integrity, selected) in PACKAGES.items():
        url = f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        if base64.b64encode(hashlib.sha512(data).digest()).decode() != integrity:
            raise ValueError(f"Archive integrity mismatch: {name}")
        files = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for item in archive.getmembers():
                relative = item.name.removeprefix("package/")
                font = name == "katex" and relative.startswith("dist/fonts/")
                if not item.isfile() or not (relative in selected or font):
                    continue
                target = ROOT / name / relative
                target.resolve().relative_to(ROOT.resolve())
                content = archive.extractfile(item).read()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                files[relative] = hashlib.sha256(content).hexdigest()
        if not set(selected).issubset(files):
            raise ValueError(f"Missing distribution files: {name}")
        manifest[name] = {
            "version": version,
            "url": url,
            "integrity": "sha512-" + integrity,
            "sha256": files,
        }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Vendored pinned renderers and licenses; all archive checks passed.")


if __name__ == "__main__":
    main()
