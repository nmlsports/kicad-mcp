"""kicad-cli wrappers: DRC, ERC, netlist export, renders. Results cached by file mtime."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .kicad import kicad_cli

CACHE = Path(os.environ.get("KICAD_MCP_CACHE", Path.home() / ".cache" / "kicad-mcp"))


def cache_dir(src: Path) -> Path:
    key = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    d = CACHE / f"{src.stem}-{key}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fresh(out: Path, src: Path, *extra: Path) -> bool:
    if not out.exists():
        return False
    newest = max(p.stat().st_mtime for p in (src, *extra) if p.exists())
    return out.stat().st_mtime >= newest


def run(args: list[str], timeout: float = 300) -> subprocess.CompletedProcess:
    cmd = [kicad_cli(), *args]
    env = dict(os.environ)
    env.setdefault("FONTCONFIG_PATH", "/opt/homebrew/etc/fonts") if os.path.isdir("/opt/homebrew/etc/fonts") else None
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-8:])
        raise RuntimeError(f"kicad-cli {' '.join(args[:2])} failed (exit {r.returncode}):\n{tail}")
    return r


def _sheet_files(sch: Path) -> list[Path]:
    """Root schematic plus sibling .kicad_sch files (sub-sheets), for cache freshness."""
    return [sch, *sorted(sch.parent.glob("*.kicad_sch"))]


def drc(pcb: Path, parity: bool = False, all_track_errors: bool = False) -> dict:
    out = cache_dir(pcb) / f"drc{'-parity' if parity else ''}{'-all' if all_track_errors else ''}.json"
    if not _fresh(out, pcb, *(_sheet_files(pcb.with_suffix('.kicad_sch')) if parity else [])):
        args = ["pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "-o", str(out)]
        if parity:
            args.append("--schematic-parity")
        if all_track_errors:
            args.append("--all-track-errors")
        run([*args, str(pcb)])
    return json.loads(out.read_text())


def erc(sch: Path) -> dict:
    out = cache_dir(sch) / "erc.json"
    if not _fresh(out, *_sheet_files(sch)):
        run(["sch", "erc", "--format", "json", "--severity-all", "--units", "mm", "-o", str(out), str(sch)])
    return json.loads(out.read_text())


def netlist_xml(sch: Path) -> Path:
    out = cache_dir(sch) / "netlist.xml"
    if not _fresh(out, *_sheet_files(sch)):
        run(["sch", "export", "netlist", "--format", "kicadxml", "-o", str(out), str(sch)])
    return out


def render_3d(pcb: Path, side: str = "top", zoom: float = 1.0, width: int = 1600, height: int = 1000,
              output: str | None = None, quality: str = "basic") -> Path:
    out = Path(output) if output else cache_dir(pcb) / f"render-{side}-z{zoom:g}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["pcb", "render", "--side", side, "--zoom", str(zoom), "--width", str(width), "--height", str(height),
         "--quality", quality, "--background", "opaque", "-o", str(out), str(pcb)])
    return out


def render_layers(pcb: Path, layers: list[str], output: str | None = None, mirror: bool = False,
                  size_px: int = 2400) -> tuple[Path, Path | None]:
    """SVG of the given layers (board-fit), plus a PNG rasterised with macOS qlmanage when available."""
    tag = "-".join(l.replace(".", "") for l in layers)[:60]
    svg = Path(output) if output and output.endswith(".svg") else cache_dir(pcb) / f"layers-{tag}{'-m' if mirror else ''}.svg"
    svg.parent.mkdir(parents=True, exist_ok=True)
    args = ["pcb", "export", "svg", "--layers", ",".join(layers), "--page-size-mode", "2",
            "--exclude-drawing-sheet", "--drill-shape-opt", "2", "-o", str(svg)]
    if mirror:
        args.append("--mirror")
    run([*args, str(pcb)])
    png = None
    if shutil.which("qlmanage"):
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["qlmanage", "-t", "-s", str(size_px), "-o", td, str(svg)], capture_output=True, timeout=120)
            produced = Path(td) / (svg.name + ".png")
            if produced.exists():
                png = Path(output) if output and output.endswith(".png") else svg.with_suffix(".png")
                shutil.move(str(produced), str(png))
    return svg, png
