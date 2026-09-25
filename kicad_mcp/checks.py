"""Summaries of DRC/ERC JSON reports."""
from __future__ import annotations

from .fmt import table


def _flatten_erc(report: dict) -> list[dict]:
    out = []
    for s in report.get("sheets", []):
        for v in s.get("violations", []):
            out.append({**v, "sheet": s.get("path", "/")})
    return out


def _flatten_drc(report: dict) -> list[dict]:
    out = []
    for key, section in (("violations", "drc"), ("unconnected_items", "unconnected"), ("schematic_parity", "parity")):
        for v in report.get(key, []) or []:
            out.append({**v, "section": section})
    return out


def _item_str(v: dict, with_pos: bool = True) -> str:
    parts = []
    for it in v.get("items", []):
        pos = it.get("pos")
        loc = f" @({pos['x']:.2f},{pos['y']:.2f})" if pos and with_pos else ""
        parts.append(f"{it.get('description', '')}{loc}")
    return "; ".join(parts)


def summarize(report: dict, kind: str, type_filter: str | None, severity: str | None, limit: int, excluded: bool) -> str:
    viol = _flatten_erc(report) if kind == "erc" else _flatten_drc(report)
    if not excluded:
        viol = [v for v in viol if not v.get("excluded")]
    if severity:
        viol = [v for v in viol if v.get("severity") == severity]
    head = []
    head.append(f"KiCad {report.get('kicad_version', '?')}, {len(viol)} {kind.upper()} findings"
                + (f" (severity={severity})" if severity else "") + ".")
    if type_filter:
        rows = [v for v in viol if v.get("type") == type_filter]
        if not rows:
            known = sorted({v.get("type") for v in viol})
            return head[0] + f"\nNo findings of type {type_filter!r}. Types present: {', '.join(known)}"
        lines = [f"{head[0]} Showing type `{type_filter}`: {len(rows)} (first {min(limit, len(rows))})."]
        for v in rows[:limit]:
            where = f" [{v['sheet']}]" if kind == "erc" and v.get("sheet") not in (None, "/") else ""
            lines.append(f"- {v.get('severity', '?')}{where}: {v.get('description', '')}\n  {_item_str(v, with_pos=kind == 'drc')}")
        return "\n".join(lines)
    groups: dict[str, dict] = {}
    for v in viol:
        g = groups.setdefault(v.get("type", "?"), {"type": v.get("type", "?"), "count": 0, "error": 0, "warning": 0, "example": ""})
        g["count"] += 1
        sev = v.get("severity", "")
        if sev in ("error", "warning"):
            g[sev] += 1
        if not g["example"]:
            g["example"] = v.get("description", "")[:110]
    rows = sorted(groups.values(), key=lambda g: (-g["error"], -g["count"]))
    out = head + [table(rows, ["type", "count", "error", "warning", "example"])]
    out.append("Call again with `type=<type>` to list the individual findings with locations.")
    if kind == "drc":
        n_unc = len(report.get("unconnected_items", []) or [])
        n_par = len(report.get("schematic_parity", []) or [])
        out.append(f"Unconnected items: {n_unc}. Schematic parity issues: {n_par}.")
    return "\n".join(out)
