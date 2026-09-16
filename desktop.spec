# Build with: python tools/build_desktop.py
from pathlib import Path

root = Path(SPECPATH)
analysis = Analysis(
    [str(root / 'desktop_entry.py')],
    pathex=[str(root / 'src')],
    binaries=[],
    datas=[(str(root / 'src/simlab/static/studio'), 'simlab/static/studio')],
    hiddenimports=['webview.platforms.edgechromium', 'webview.platforms.winforms',
                   'uvicorn.logging', 'uvicorn.loops.asyncio', 'uvicorn.protocols.http.h11_impl',
                   'uvicorn.lifespan.on'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'pandas', 'scipy', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
              'gtk', 'gi', 'cefpython3', 'pytest', 'IPython', 'notebook', 'tkinter', 'mcp'],
    noarchive=False,
)
archive = PYZ(analysis.pure)
exe = EXE(
    archive, analysis.scripts, analysis.binaries, analysis.datas, [],
    name='SimPy Lab Studio', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    icon=str(root / 'src/simlab/static/studio/app.ico'),
    version=str(root / 'tools/desktop_version.txt'),
)
