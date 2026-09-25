"""Locate the KiCad installation: kicad-cli and the Python that can import pcbnew."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_MAC_APPS = [
    Path("/Applications/KiCad/KiCad.app"),
    Path.home() / "Applications/KiCad/KiCad.app",
]


def _mac_app() -> Path | None:
    env = os.environ.get("KICAD_APP")
    candidates = ([Path(env)] if env else []) + _MAC_APPS
    for c in candidates:
        if (c / "Contents/MacOS/kicad-cli").exists():
            return c
    return None


def kicad_cli() -> str:
    env = os.environ.get("KICAD_CLI")
    if env:
        return env
    app = _mac_app()
    if app:
        return str(app / "Contents/MacOS/kicad-cli")
    found = shutil.which("kicad-cli")
    if found:
        return found
    raise RuntimeError(
        "kicad-cli not found. Install KiCad, or set KICAD_CLI (or KICAD_APP on macOS)."
    )


def kicad_python() -> str:
    """Interpreter able to `import pcbnew` (KiCad's bundled Python on macOS)."""
    env = os.environ.get("KICAD_PYTHON")
    if env:
        return env
    app = _mac_app()
    if app:
        py = app / "Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
        if py.exists():
            return str(py)
    # Linux distro packages install pcbnew into the system python.
    for cand in ("python3", sys.executable):
        exe = shutil.which(cand) or cand
        if exe and os.path.exists(exe):
            return exe
    raise RuntimeError("No Python with pcbnew found. Set KICAD_PYTHON.")


def version() -> str:
    import subprocess

    return subprocess.run([kicad_cli(), "version"], capture_output=True, text=True).stdout.strip()
