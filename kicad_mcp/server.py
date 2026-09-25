"""MCP server: compact, read-only access to KiCad boards and schematics."""
import functools
from collections import Counter
from pathlib import Path
from typing import Optional

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import checks, cli, netlist as nlmod
from .fmt import clip, kv, num, table
from .paths import project_dir, project_file, resolve_pcb, resolve_sch
from .worker_client import WorkerError, worker

mcp = FastMCP(
    "kicad",
    instructions=(
        "Read-only access to KiCad projects. `path` may be a project directory, a .kicad_pro, a .kicad_pcb "
        "or a .kicad_sch; it defaults to the current directory. Units are millimetres, KiCad coordinates "
        "(y grows downward). Start with project_info, then board_summary / schematic_summary, then drill in. "
        "Prefer these tools over reading .kicad_* files directly."
    ),
)


def _tool(fn):
    """Register a tool whose exceptions become short error strings instead of tracebacks."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except (WorkerError, FileNotFoundError, KeyError, RuntimeError, ValueError) as e:
            msg = str(e)
            if isinstance(e, KeyError) and msg.startswith(("'", '"')):
                msg = msg[1:-1]
            return f"Error: {msg}"
    return mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))(wrapper)


def _nl(path: Optional[str]) -> tuple[Path, nlmod.Netlist]:
    sch = resolve_sch(path)
    return sch, nlmod.parse(cli.netlist_xml(sch))


# ------------------------------------------------------------------ project


@_tool
def project_info(path: Optional[str] = None) -> str:
    """Overview of a KiCad project folder: files, board size and counts, schematic sheets. Start here."""
    d = project_dir(path)
    pro = project_file(path)
    lines = [f"Project dir: {d}", f"Project file: {pro.name if pro else '(none)'}"]
    files = sorted(p.name for p in d.iterdir() if p.suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru"))
    lines.append("Files: " + ", ".join(files))
    for tbl in ("sym-lib-table", "fp-lib-table"):
        f = d / tbl
        if f.exists():
            import re
            names = re.findall(r'\(name\s+"?([^")\s]+)"?\)', f.read_text(errors="ignore"))
            lines.append(f"{tbl}: {', '.join(names) or '(empty)'}")
    try:
        pcb = resolve_pcb(str(d))
        s = worker().call("board_summary", pcb=str(pcb))
        o = s["outline"]
        fp = s["footprints"]
        lines.append(f"Board: {pcb.name}: {num(o['w'])} x {num(o['h'])} mm, {len(s['copper_layers'])} copper layers, "
                     f"{fp['total']} footprints ({fp['dnp']} DNP), {s['nets']} nets, {s['tracks']['count']} tracks, {s['vias']['count']} vias")
    except (FileNotFoundError, WorkerError) as e:
        lines.append(f"Board: {e}")
    try:
        sch, nl = _nl(str(d))
        lines.append(f"Schematic: {sch.name}: {len(nl.sheets)} sheet(s), {len(nl.components)} symbols, {len(nl.nets)} nets")
        for sh in nl.sheets:
            lines.append(f"  - sheet {sh['number']}: {sh['name']}  ({sh['file']})")
    except (FileNotFoundError, RuntimeError) as e:
        lines.append(f"Schematic: {e}")
    return "\n".join(lines)


# ------------------------------------------------------------------ board


@_tool
def board_summary(path: Optional[str] = None) -> str:
    """Board overview: outline, layers, footprint/net/track/via counts, zones, design rules and netclasses."""
    s = worker().call("board_summary", pcb=str(resolve_pcb(path)))
    o = s["outline"]
    fp = s["footprints"]
    t = s["tracks"]
    out = [
        kv({
            "file": s["file"],
            "title / rev / date": " / ".join(x for x in (s["title"], s["revision"], s["date"]) if x) or None,
            "outline": f"{num(o['w'])} x {num(o['h'])} mm at ({num(o['x'])}, {num(o['y'])}), thickness {num(s['board_thickness'])} mm",
            "copper layers": ", ".join(s["copper_layers"]),
            "other layers": ", ".join(s["other_layers"]),
            "footprints": f"{fp['total']} (front {fp['front']}, back {fp['back']}, SMD {fp['smd']}, THT {fp['tht']}, DNP {fp['dnp']})",
            "nets": s["nets"],
            "tracks": f"{t['count']} segments, {num(t['total_length_mm'])} mm total: "
                      + ", ".join(f"{k} {num(v)}" for k, v in t["length_by_layer_mm"].items()),
            "vias": f"{s['vias']['count']} " + str(s["vias"]["by_type"]),
            "drawings by layer": ", ".join(f"{k} {v}" for k, v in s["drawings_by_layer"].items()),
        }),
        "Rules (mm): " + ", ".join(f"{k} {num(v)}" for k, v in s["rules"].items()),
        "Netclasses:",
        table(s["netclasses"], ["name", "clearance", "track_width", "via_dia", "via_drill"]),
        "Zones:",
        table(s["zones"], ["net", "name", "layers", "priority", "filled_area_mm2", "rule_area"]),
    ]
    return "\n".join(out)


@_tool
def list_components(path: Optional[str] = None, ref: Optional[str] = None, value: Optional[str] = None,
                    footprint: Optional[str] = None, side: Optional[str] = None, dnp: Optional[bool] = None,
                    limit: int = 100, offset: int = 0) -> str:
    """List footprints on the board with position (mm), rotation, side. Filters are case-insensitive
    substrings, or globs if they contain * ? [ (e.g. ref="C*", value="10k", footprint="0402", side="F")."""
    r = worker().call("list_components", pcb=str(resolve_pcb(path)), ref=ref, value=value, footprint=footprint,
                      side=side, dnp=dnp, limit=limit, offset=offset)
    return (table(r["rows"], ["ref", "value", "footprint", "x", "y", "rot", "side", "type", "dnp", "pads"])
            + clip(r["rows"], limit, r["total"]))


@_tool
def get_component(path: Optional[str] = None, ref: str = "") -> str:
    """Full detail for one footprint by reference: position, bbox, courtyard, fields, 3D model, every pad with net."""
    c = worker().call("get_component", pcb=str(resolve_pcb(path)), ref=ref)
    bb, cy = c["bbox"], c["courtyard"]
    head = kv({
        "ref": f"{c['ref']}  {c['value']}  [{c['footprint']}]",
        "position": f"({num(c['x'])}, {num(c['y'])}) rot {num(c['rot'])}° on {c['layer']} ({c['side']} side), {c['type']}",
        "flags": ", ".join(k for k, v in (("DNP", c["dnp"]), ("excluded from BOM", c["exclude_from_bom"]),
                                          ("excluded from pos", c["exclude_from_pos"])) if v) or "none",
        "bbox": f"{num(bb['w'])} x {num(bb['h'])} mm at ({num(bb['x'])}, {num(bb['y'])})",
        "courtyard": f"{num(cy['w'])} x {num(cy['h'])} mm at ({num(cy['x'])}, {num(cy['y'])})" if cy else "none",
        "sheet": c["sheet"],
        "description": c["description"],
        "fields": ", ".join(f"{k}={v}" for k, v in c["fields"].items() if k not in ("Reference", "Value")),
        "3D models": ", ".join(c["models"]),
    })
    pads = table(c["pads_detail"], ["pad", "net", "pin", "type", "shape", "w", "h", "drill", "layers", "x", "y"])
    return f"{head}\nPads ({len(c['pads_detail'])}):\n{pads}"


@_tool
def measure(path: Optional[str] = None, a: str = "", b: str = "") -> str:
    """Distance between two items. Each of a/b is a footprint ref ("U7"), a pad ("U7.3" or "J1:A6"), or a
    point in mm ("120.5,44"). Returns dx/dy, centre distance, compass direction, and edge-to-edge gap
    (courtyard-to-courtyard for footprints, copper-to-copper for pads)."""
    r = worker().call("measure", pcb=str(resolve_pcb(path)), a=a, b_item=b)
    return kv({
        "a": r["a"], "b": r["b"],
        "centre distance": f"{num(r['center_distance'])} mm (dx {num(r['dx'])}, dy {num(r['dy'])}), b is {r['direction_a_to_b']} of a",
        "edge gap": f"{num(r['edge_gap'])} mm ({r['edge_gap_basis']})" if r.get("edge_gap") is not None else None,
        "bbox gap": f"{num(r['bbox_gap'])} mm" if r.get("bbox_gap") is not None else None,
        "same side": r.get("same_side"),
    })


@_tool
def neighbors(path: Optional[str] = None, ref: str = "", radius: float = 5.0, limit: int = 20) -> str:
    """Footprints within `radius` mm of a footprint, nearest first, with courtyard gap and direction."""
    r = worker().call("neighbors", pcb=str(resolve_pcb(path)), ref=ref, radius=radius, limit=limit)
    head = f"{r['ref']} at ({num(r['pos']['x'])}, {num(r['pos']['y'])}) {r['side']} side; {r['total']} footprints within {num(radius)} mm:"
    return head + "\n" + table(r["rows"], ["ref", "value", "side", "dir", "center_mm", "gap_mm", "gap_basis"]) + clip(r["rows"], limit, r["total"])


@_tool
def net_info(path: Optional[str] = None, net: str = "") -> str:
    """Everything on one net: pads (with pin names), track length per layer, widths, vias, zones, netclass rules.
    Net name may be exact, case-insensitive, or a unique substring."""
    r = worker().call("net_info", pcb=str(resolve_pcb(path)), net=net)
    t, v = r["tracks"], r["vias"]
    e = r.get("extent")
    head = kv({
        "net": f"{r['name']} (code {r['code']})",
        "netclass": f"{r.get('netclass')}: clearance {num(r.get('clearance'))}, track {num(r.get('track_width'))}, via {num(r.get('via_dia'))}/{num(r.get('via_drill'))} mm" if r.get("netclass") else None,
        "tracks": f"{t['count']} segments, {num(t['total_length_mm'])} mm: " + ", ".join(f"{k} {num(x)}" for k, x in t["by_layer_mm"].items())
                  + (f"; widths {', '.join(num(w) for w in t['widths_mm'])} mm" if t["widths_mm"] else ""),
        "vias": f"{v['count']}" + (f" (drill {', '.join(num(d) for d in v['drills_mm'])} mm)" if v["drills_mm"] else ""),
        "zones": ", ".join(r["zones"]) or None,
        "extent": f"{num(e['w'])} x {num(e['h'])} mm at ({num(e['x'])}, {num(e['y'])})" if e else None,
    })
    return f"{head}\nPads ({len(r['pads'])}):\n" + table(r["pads"], ["ref", "pad", "pin", "pin_type", "layers", "x", "y"])


@_tool
def list_nets(path: Optional[str] = None, filter: Optional[str] = None, limit: int = 100, sort: str = "pads") -> str:
    """Nets on the board with pad count, routed length and via count. sort = pads | length | name."""
    r = worker().call("list_nets", pcb=str(resolve_pcb(path)), filter=filter, limit=limit, sort=sort)
    return table(r["rows"], ["net", "pads", "track_mm", "vias", "netclass"]) + clip(r["rows"], limit, r["total"])


@_tool
def items_in_region(path: Optional[str] = None, x1: float = 0, y1: float = 0, x2: float = 0, y2: float = 0,
                    layer: Optional[str] = None) -> str:
    """What lies inside a rectangle (mm corners): footprints, tracks per net/layer, vias, zones, text.
    Optional layer name (e.g. "F.Cu", "B.Silkscreen") restricts the answer."""
    r = worker().call("items_in_region", pcb=str(resolve_pcb(path)), x1=x1, y1=y1, x2=x2, y2=y2, layer=layer)
    g = r["region"]
    out = [f"Region {num(g['w'])} x {num(g['h'])} mm at ({num(g['x'])}, {num(g['y'])})" + (f", layer {layer}" if layer else "")]
    out.append(f"Footprints ({len(r['footprints'])}):\n" + table(r["footprints"], ["ref", "value", "x", "y", "side"]))
    out.append(f"Tracks:\n" + table(r["tracks"][:40], ["net", "layer", "segments", "length_mm"]))
    out.append(f"Vias: {r['vias']}; zones: {', '.join(r['zones']) or 'none'}")
    if r["texts"]:
        out.append("Text: " + "; ".join(f"\"{t['text']}\" ({t['layer']})" for t in r["texts"]))
    if r["drawings_by_layer"]:
        out.append("Drawings: " + ", ".join(f"{k} {v}" for k, v in r["drawings_by_layer"].items()))
    return "\n".join(out)


@_tool
def search(path: Optional[str] = None, query: str = "", limit: int = 50) -> str:
    """Find where a string occurs on the board: references, values, footprint names, fields, net names, silkscreen text."""
    r = worker().call("search", pcb=str(resolve_pcb(path)), query=query, limit=limit)
    return table(r["rows"], ["kind", "item", "match", "x", "y", "side"]) + clip(r["rows"], limit, r["total"])


@_tool
def drc(path: Optional[str] = None, type: Optional[str] = None, severity: Optional[str] = None,
        parity: bool = False, limit: int = 50, include_excluded: bool = False) -> str:
    """Run DRC (cached until the board changes). Without `type`: counts per violation type. With `type`:
    individual findings with positions. severity = error | warning. parity=true also checks against the schematic."""
    pcb = resolve_pcb(path)
    report = cli.drc(pcb, parity=parity)
    return checks.summarize(report, "drc", type, severity, limit, include_excluded)


@_tool
def render_3d(path: Optional[str] = None, side: str = "top", zoom: float = 1.0, output: Optional[str] = None) -> str:
    """Render the board to a PNG (raytraced-lite) and return its path so you can view it. side = top | bottom |
    left | right | front | back. zoom > 1 magnifies the centre."""
    p = cli.render_3d(resolve_pcb(path), side=side, zoom=zoom, output=output)
    return f"Rendered {side} view to {p}"


@_tool
def render_layers(path: Optional[str] = None, layers: str = "F.Cu,Edge.Cuts", mirror: bool = False,
                  output: Optional[str] = None) -> str:
    """2D plot of chosen layers (comma-separated, e.g. "F.Cu,F.Silkscreen,Edge.Cuts") fitted to the board.
    Returns the SVG path and, on macOS, a PNG path you can view. mirror=true for looking at the bottom."""
    svg, png = cli.render_layers(resolve_pcb(path), [l.strip() for l in layers.split(",") if l.strip()],
                                 output=output, mirror=mirror)
    return f"SVG: {svg}" + (f"\nPNG: {png}" if png else "\n(no PNG: qlmanage not available)")


# ------------------------------------------------------------------ schematic


@_tool
def schematic_summary(path: Optional[str] = None) -> str:
    """Schematic overview from the exported netlist: sheets, symbol counts by prefix, nets, power nets, DNP parts."""
    sch, nl = _nl(path)
    prefixes = Counter(nlmod.ref_key(r)[0] for r in nl.components)
    dnp = [r for r, c in nl.components.items() if c["dnp"]]
    nobom = [r for r, c in nl.components.items() if c["exclude_from_bom"]]
    power = sorted(n for n in nl.nets if n.startswith(("+", "-", "GND", "VCC", "VDD", "VBUS", "VBAT")) or n.endswith(("V", "GND")))
    no_fp = [r for r, c in nl.components.items() if not c["footprint"] and not c["exclude_from_board"]]
    out = [
        f"Root: {sch}",
        "Sheets:\n" + table(nl.sheets, ["number", "name", "file", "title", "rev"]),
        f"Symbols: {len(nl.components)} (" + ", ".join(f"{k} {v}" for k, v in sorted(prefixes.items())) + ")",
        f"Nets: {len(nl.nets)}; power-ish nets: {', '.join(power[:30]) or 'none'}",
        f"DNP: {', '.join(sorted(dnp, key=nlmod.ref_key)) or 'none'}",
        f"Excluded from BOM: {', '.join(sorted(nobom, key=nlmod.ref_key)) or 'none'}",
    ]
    if no_fp:
        out.append(f"Symbols without footprint: {', '.join(sorted(no_fp, key=nlmod.ref_key))}")
    return "\n".join(out)


@_tool
def sch_components(path: Optional[str] = None, ref: Optional[str] = None, value: Optional[str] = None,
                   sheet: Optional[str] = None, limit: int = 100) -> str:
    """List schematic symbols with value, footprint, sheet and DNP flag. Filters are substrings or globs."""
    from fnmatch import fnmatchcase

    def ok(v, pat):
        if not pat:
            return True
        return fnmatchcase(v.lower(), pat.lower()) if any(ch in pat for ch in "*?[") else pat.lower() in v.lower()

    _, nl = _nl(path)
    rows = [c for c in nl.components.values() if ok(c["ref"], ref) and ok(c["value"], value) and ok(c["sheet"], sheet)]
    rows.sort(key=lambda c: nlmod.ref_key(c["ref"]))
    return table(rows[:limit], ["ref", "value", "footprint", "sheet", "dnp"]) + clip(rows, limit)


@_tool
def sch_component(path: Optional[str] = None, ref: str = "") -> str:
    """One schematic symbol: fields, library part, sheet, and every pin with the net it is on (blank = unconnected)."""
    _, nl = _nl(path)
    c = nl.components.get(ref) or next((x for r, x in nl.components.items() if r.lower() == ref.lower()), None)
    if not c:
        return f"Error: no symbol {ref!r} in schematic"
    pins = nl.pins_of(c["ref"])
    head = kv({
        "ref": f"{c['ref']}  {c['value']}",
        "lib part": f"{c['lib']}:{c['part']}" + (f"  ({c['description']})" if c["description"] else ""),
        "footprint": c["footprint"] or "(none)",
        "sheet": c["sheet"],
        "flags": ", ".join(k for k, v in (("DNP", c["dnp"]), ("excluded from BOM", c["exclude_from_bom"]),
                                          ("excluded from board", c["exclude_from_board"])) if v) or "none",
        "datasheet": c["datasheet"],
        "fields": ", ".join(f"{k}={v}" for k, v in c["fields"].items() if v),
    })
    unconnected = [p["num"] for p in pins if not p["net"]]
    return (f"{head}\nPins ({len(pins)}, {len(unconnected)} unconnected):\n"
            + table(pins, ["num", "name", "type", "net"]))


@_tool
def sch_net(path: Optional[str] = None, net: str = "") -> str:
    """All pins on a schematic net (exact, case-insensitive, or unique-substring name)."""
    _, nl = _nl(path)
    n = nl.nets.get(net)
    if not n:
        lname = net.lower()
        cands = [x for k, x in nl.nets.items() if k.lower() == lname]
        note = ""
        if not cands:
            cands = [x for k, x in nl.nets.items() if k.lower().rsplit("/", 1)[-1] == lname]
            if len(cands) > 1:  # same label on several sheets: take the shallowest, say so
                cands.sort(key=lambda x: (x["name"].count("/"), x["name"]))
                note = " (also on other sheets: " + ", ".join(c["name"] for c in cands[1:6]) + ")"
                cands = cands[:1]
        if not cands:
            cands = [x for k, x in nl.nets.items() if lname in k.lower()]
        if len(cands) != 1:
            return f"Error: {'no net matching' if not cands else 'ambiguous net'} {net!r}" + (
                ": " + ", ".join(c["name"] for c in cands[:15]) if cands else "")
        n = cands[0]
        n = dict(n, name=n["name"] + note)
    nodes = sorted(n["nodes"], key=lambda d: (nlmod.ref_key(d["ref"]), d["pin"]))
    for d in nodes:
        d["value"] = nl.components.get(d["ref"], {}).get("value", "")
    return f"Net {n['name']} (code {n['code']}), {len(nodes)} pins:\n" + table(nodes, ["ref", "value", "pin", "function", "type"])


@_tool
def sch_nets(path: Optional[str] = None, filter: Optional[str] = None, limit: int = 100) -> str:
    """Schematic nets with pin counts, largest first. filter is a case-insensitive substring."""
    _, nl = _nl(path)
    rows = [{"net": n["name"], "pins": len(n["nodes"]),
             "refs": ", ".join(sorted({d["ref"] for d in n["nodes"]}, key=nlmod.ref_key)[:12])}
            for n in nl.nets.values() if not filter or filter.lower() in n["name"].lower()]
    rows.sort(key=lambda r: (-r["pins"], r["net"]))
    return table(rows[:limit], ["net", "pins", "refs"]) + clip(rows, limit)


@_tool
def bom(path: Optional[str] = None, include_dnp: bool = False, group_by_footprint: bool = True) -> str:
    """Bill of materials grouped by value (and footprint): quantity, references, and part-number fields."""
    _, nl = _nl(path)
    groups: dict[tuple, dict] = {}
    skipped = 0
    for c in sorted(nl.components.values(), key=lambda c: nlmod.ref_key(c["ref"])):
        if c["exclude_from_bom"] or (c["dnp"] and not include_dnp):
            skipped += 1
            continue
        key = (c["value"], c["footprint"] if group_by_footprint else "")
        g = groups.setdefault(key, {"value": c["value"], "footprint": c["footprint"].split(":")[-1], "qty": 0, "refs": [], "part": ""})
        g["qty"] += 1
        g["refs"].append(c["ref"] + ("*" if c["dnp"] else ""))
        if not g["part"]:
            for k, v in c["fields"].items():
                if v and any(t in k.lower() for t in ("mpn", "part", "digikey", "mouser", "lcsc")):
                    g["part"] = v
                    break
    rows = list(groups.values())
    for g in rows:
        g["refs"] = ", ".join(g["refs"])
    rows.sort(key=lambda g: nlmod.ref_key(g["refs"].split(",")[0]))
    return (f"{len(rows)} line items, {sum(g['qty'] for g in rows)} parts; {skipped} skipped (DNP/excluded)"
            + (" (* = DNP)" if include_dnp else "") + "\n" + table(rows, ["qty", "value", "footprint", "refs", "part"], ["qty", "value", "footprint", "refs", "part no."]))


@_tool
def erc(path: Optional[str] = None, type: Optional[str] = None, severity: Optional[str] = None,
        limit: int = 50, include_excluded: bool = False) -> str:
    """Run ERC on the schematic (cached until it changes). Without `type`: counts per type. With `type`:
    individual findings with sheet and position. severity = error | warning."""
    report = cli.erc(resolve_sch(path))
    return checks.summarize(report, "erc", type, severity, limit, include_excluded)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
