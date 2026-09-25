"""Resolve user-supplied paths to a board (.kicad_pcb) or root schematic (.kicad_sch)."""
from __future__ import annotations

import os
from pathlib import Path


def _expand(path: str | None) -> Path:
    return Path(os.path.expanduser(path or ".")).resolve()


def project_dir(path: str | None = None) -> Path:
    p = _expand(path)
    return p if p.is_dir() else p.parent


def project_file(path: str | None = None) -> Path | None:
    d = project_dir(path)
    p = _expand(path)
    if p.suffix == ".kicad_pro":
        return p
    pros = sorted(d.glob("*.kicad_pro"))
    return pros[0] if pros else None


def _resolve(path: str | None, suffix: str, what: str) -> Path:
    p = _expand(path)
    if p.is_file():
        if p.suffix == suffix:
            return p
        if p.suffix == ".kicad_pro":
            cand = p.with_suffix(suffix)
            if cand.exists():
                return cand
        cand = p.with_suffix(suffix)
        if cand.exists():
            return cand
        raise FileNotFoundError(f"{p} is not a {what} and no {suffix} file sits beside it")
    if not p.is_dir():
        raise FileNotFoundError(f"{p} does not exist")
    pro = project_file(str(p))
    if pro:
        cand = pro.with_suffix(suffix)
        if cand.exists():
            return cand
    matches = sorted(p.glob(f"*{suffix}"))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"No {suffix} file in {p}")
    raise FileNotFoundError(
        f"Several {suffix} files in {p}: {', '.join(m.name for m in matches)}. Pass one explicitly."
    )


def resolve_pcb(path: str | None = None) -> Path:
    return _resolve(path, ".kicad_pcb", "board")


def resolve_sch(path: str | None = None) -> Path:
    """Root schematic: the one matching the .kicad_pro stem, else the single .kicad_sch."""
    return _resolve(path, ".kicad_sch", "schematic")
