# -*- mode: python ; coding: utf-8 -*-
"""F-Pulse OSS — PyInstaller spec (Windows onedir freeze).

Produces ``dist/fpulse/fpulse.exe`` — a self-contained CLI + server that the
Inno Setup script (fpulse.iss) packages into the installer.

Run from the repo root:
    .venv\\Scripts\\python -m PyInstaller installer\\windows\\fpulse.spec --noconfirm --clean

Why a spec instead of the inline ``--collect-all`` flags: this app pulls in
pandas / pyarrow / duckdb / cryptography / reportlab / tzdata / fastavro and
loads several nodes + connectors by NAME at runtime. A spec lets us collect
their data files (the 46 connector JSONs, the IANA tz database, reportlab
fonts, python-docx templates) and submodules reliably, which the 4-package
inline command did not. Validate the result with: ``fpulse.exe selftest``.
"""

import os

# SPECPATH is injected by PyInstaller = this file's directory (installer/windows).
HERE = SPECPATH
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BACKEND = os.path.join(REPO, "backend")
ENTRY = os.path.join(BACKEND, "fpulse", "__main__.py")
ICON = os.path.join(HERE, "icons", "fpulse.ico")

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# Collect WHOLESALE (code + data files + native libs + submodules) for the
# packages that ship data/native payloads or that we import dynamically.
_COLLECT = [
    "fpulse",        # our app — brings connectors/*.json, static/, seed_data/, every submodule
    "duckdb",        # native engine lib
    "pyarrow",       # native + data
    "pandas",
    "numpy",
    "fastavro",      # Avro reader
    "tzdata",        # IANA tz db — pyarrow.orc requires it on Windows
    "reportlab",     # bundled fonts (PDF reports)
    "docx",          # python-docx default template (DOCX reports)
    "openpyxl",
    "pydantic",
    "cryptography",
    "webview",       # pywebview — native `fpulse app` window (its JS bridge files)
    "clr_loader",    # pythonnet runtime loader used by the WebView2 backend
]
for pkg in _COLLECT:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# pywebview's Windows backend loads via pythonnet (clr); these are imported
# dynamically and must be forced in. If the freeze ever can't bundle them,
# `fpulse app` still falls back to a Chromium --app window at runtime.
hiddenimports += [
    "clr", "webview.platforms.edgechromium", "webview.platforms.winforms",
]

# Submodules imported by name (PyInstaller's static analysis can miss these).
for pkg in ("fpulse", "uvicorn", "fastapi", "starlette", "anyio"):
    hiddenimports += collect_submodules(pkg)

# uvicorn[standard] picks its loop/http/ws implementations at runtime, plus a
# few other runtime/dynamic deps that aren't statically referenced.
hiddenimports += [
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "httptools", "websockets", "watchfiles", "dotenv",
    "bcrypt", "psutil", "httpx", "requests", "aiofiles",
    "multipart", "yaml", "prometheus_client", "fastavro._read_py",
]

# De-dup while preserving order.
hiddenimports = list(dict.fromkeys(hiddenimports))

a = Analysis(
    [ENTRY],
    pathex=[BACKEND],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "IPython", "jupyter", "notebook", "pytest", "_pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # onedir: keep libs beside the exe (COLLECT below)
    name="fpulse",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX can trip antivirus heuristics; keep off
    console=True,               # fpulse is a CLI/server
    disable_windowed_traceback=False,
    icon=(ICON if os.path.exists(ICON) else None),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="fpulse",
)
