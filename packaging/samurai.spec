# PyInstaller spec for the standalone `samurai` binary.
# Build: uv run --group release pyinstaller packaging/samurai.spec

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("textual")

a = Analysis(
    ["../src/samurai_ai/__main__.py"],
    pathex=["../src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "mypy", "ruff"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="samurai",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
