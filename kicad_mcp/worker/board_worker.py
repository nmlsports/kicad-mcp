"""Board query worker. Runs under KiCad's bundled Python (3.9), speaks JSON lines on stdio.

Request:  {"id": 1, "method": "board_summary", "params": {"pcb": "/path/x.kicad_pcb"}}
Response: {"id": 1, "result": ...}  or  {"id": 1, "error": "...", "trace": "..."}

Everything else that would land on stdout is redirected to stderr so the channel stays clean.
Must stay Python 3.9 compatible.
"""
import fnmatch
import json
import math
import os
import re
import sys
import traceback

_out = sys.stdout
sys.stdout = sys.stderr

import pcbnew  # noqa: E402

# ---------------------------------------------------------------- helpers


def mm(v):
    return round(pcbnew.ToMM(int(v)), 4)


def from_mm(v):
    return pcbnew.FromMM(float(v))


_boards = {}


def load(path):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    mt = os.path.getmtime(path)
    ent = _boards.get(path)
    if ent and ent[0] == mt:
        return ent[1]
    b = pcbnew.LoadBoard(path)
    _boards[path] = (mt, b)
    return b


_REF_RE = re.compile(r"^([A-Za-z_#]+)(\d*)(.*)$")


def ref_key(ref):
    m = _REF_RE.match(ref or "")
    if not m:
        return (ref, 0, "")
    return (m.group(1).upper(), int(m.group(2) or 0), m.group(3))


def glob_match(value, pattern):
    if not pattern:
        return True
    v = str(value)
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(v.lower(), pattern.lower())
    return pattern.lower() in v.lower()


def bbox_dict(bb):
    return {"x": mm(bb.GetLeft()), "y": mm(bb.GetTop()), "w": mm(bb.GetWidth()), "h": mm(bb.GetHeight())}


def bbox_gap(a, b):
    """Edge-to-edge gap between two BOX2I (0 if they overlap)."""
    dx = max(a.GetLeft() - b.GetRight(), b.GetLeft() - a.GetRight(), 0)
    dy = max(a.GetTop() - b.GetBottom(), b.GetTop() - a.GetBottom(), 0)
    return mm(math.hypot(dx, dy))


def shape_gap(sa, sb, max_mm=500.0):
    """Edge-to-edge distance between two SHAPEs via binary search on Collide clearance."""
    if sa is None or sb is None:
        return None
    if sa.Collide(sb, 0):
        return 0.0
    lo, hi = 0, from_mm(max_mm)
    if not sa.Collide(sb, hi):
        return None
    while hi - lo > 50:  # 50 nm
        mid = (lo + hi) // 2
        if sa.Collide(sb, mid):
            hi = mid
        else:
            lo = mid
    return mm(hi)


def point_gap(shape, pt, max_mm=500.0):
    if shape is None:
        return None
    if shape.Collide(pt, 0):
        return 0.0
    lo, hi = 0, from_mm(max_mm)
    if not shape.Collide(pt, hi):
        return None
    while hi - lo > 50:
        mid = (lo + hi) // 2
        if shape.Collide(pt, mid):
            hi = mid
        else:
            lo = mid
    return mm(hi)


def bearing(dx, dy):
    """Compass direction with KiCad's y-down convention."""
    if dx == 0 and dy == 0:
        return "-"
    ang = (math.degrees(math.atan2(-dy, dx)) + 360) % 360
    dirs = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
    return dirs[int((ang + 22.5) // 45) % 8]


def net_pads(b, netcode):
    return [p for p in b.GetPads() if p.GetNetCode() == netcode]


_PAD_SHAPES = {}
for _name in dir(pcbnew):
    if _name.startswith("PAD_SHAPE_"):
        _PAD_SHAPES[getattr(pcbnew, _name)] = _name[len("PAD_SHAPE_"):].lower()
_PAD_ATTRS = {}
for _name in dir(pcbnew):
    if _name.startswith("PAD_ATTRIB_"):
        _PAD_ATTRS[getattr(pcbnew, _name)] = _name[len("PAD_ATTRIB_"):]
_VIA_TYPES = {}
for _name in dir(pcbnew):
    if _name.startswith("VIATYPE_"):
        _VIA_TYPES[getattr(pcbnew, _name)] = _name[len("VIATYPE_"):].lower()


def pad_copper_layer(pad):
    for l in pad.GetLayerSet().Seq():
        if pcbnew.IsCopperLayer(l):
            return l
    return pcbnew.F_Cu


def pad_dict(b, p, with_pos=True):
    fp = p.GetParentFootprint()
    layer = pad_copper_layer(p)
    try:
        size = p.GetSize(layer)
    except TypeError:
        size = p.GetSize()
    try:
        shape = p.GetShape(layer)
    except TypeError:
        shape = p.GetShape()
    d = {
        "ref": fp.GetReference() if fp else "",
        "pad": str(p.GetNumber()),
        "net": str(p.GetNetname()),
        "pin": str(p.GetPinFunction()),
        "pin_type": str(p.GetPinType()),
        "type": _PAD_ATTRS.get(p.GetAttribute(), str(p.GetAttribute())),
        "shape": _PAD_SHAPES.get(shape, str(shape)),
        "w": mm(size.x),
        "h": mm(size.y),
        "drill": mm(p.GetDrillSize().x) if p.GetDrillSize().x else None,
        "layers": ",".join(b.GetLayerName(l) for l in p.GetLayerSet().Seq() if pcbnew.IsCopperLayer(l)),
    }
    if with_pos:
        pos = p.GetPosition()
        d["x"], d["y"] = mm(pos.x), mm(pos.y)
    return d


def fp_row(b, f):
    p = f.GetPosition()
    attrs = f.GetAttributes()
    return {
        "ref": str(f.GetReference()),
        "value": str(f.GetValue()),
        "footprint": str(f.GetFPIDAsString()),
        "x": mm(p.x),
        "y": mm(p.y),
        "rot": round(f.GetOrientationDegrees(), 2),
        "side": "B" if f.IsFlipped() else "F",
        "type": "SMD" if attrs & pcbnew.FP_SMD else ("THT" if attrs & pcbnew.FP_THROUGH_HOLE else "other"),
        "dnp": bool(f.IsDNP()),
        "pads": f.GetPadCount(),
    }


def fp_courtyard(f):
    layer = pcbnew.B_CrtYd if f.IsFlipped() else pcbnew.F_CrtYd
    try:
        c = f.GetCourtyard(layer)
        if c.OutlineCount() > 0:
            return c
    except Exception:
        pass
    return None


def fp_copper_shape(f):
    layer = pcbnew.B_Cu if f.IsFlipped() else pcbnew.F_Cu
    return f.GetEffectiveShape(layer)


def find_fp(b, ref):
    f = b.FindFootprintByReference(ref)
    if f is None:
        for cand in b.GetFootprints():
            if str(cand.GetReference()).lower() == ref.lower():
                return cand
        raise KeyError("No footprint with reference %r" % ref)
    return f


def find_net(b, name):
    n = b.FindNet(name)
    if n is not None and n.GetNetCode() > 0:
        return n
    lname = name.lower()
    nets = [ni for ni in b.GetNetInfo().NetsByNetcode().values() if ni.GetNetCode() > 0]
    exact = [ni for ni in nets if str(ni.GetNetname()).lower() == lname]
    if len(exact) == 1:
        return exact[0]
    tail = [ni for ni in nets if str(ni.GetNetname()).lower().rsplit("/", 1)[-1] == lname]
    if tail:  # same label on several sheets: shallowest path wins
        return sorted(tail, key=lambda ni: (str(ni.GetNetname()).count("/"), str(ni.GetNetname())))[0]
    sub = [ni for ni in nets if lname in str(ni.GetNetname()).lower()]
    if len(sub) == 1:
        return sub[0]
    if sub:
        raise KeyError("Ambiguous net %r: %s" % (name, ", ".join(str(ni.GetNetname()) for ni in sub[:10])))
    raise KeyError("No net matching %r" % name)


def netclass_for(b, netname):
    try:
        ns = b.GetDesignSettings().m_NetSettings
        nc = ns.GetEffectiveNetClass(netname)
        return {
            "netclass": str(nc.GetName()),
            "clearance": mm(nc.GetClearance()),
            "track_width": mm(nc.GetTrackWidth()),
            "via_dia": mm(nc.GetViaDiameter()),
            "via_drill": mm(nc.GetViaDrill()),
        }
    except Exception:
        return {}


def all_nets(b):
    return [ni for ni in b.GetNetInfo().NetsByNetcode().values() if ni.GetNetCode() > 0]


# ---------------------------------------------------------------- methods


def board_summary(pcb):
    b = load(pcb)
    ds = b.GetDesignSettings()
    fps = list(b.GetFootprints())
    tracks = [t for t in b.GetTracks() if t.Type() in (pcbnew.PCB_TRACE_T, pcbnew.PCB_ARC_T)]
    vias = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
    edge = b.GetBoardEdgesBoundingBox()
    if edge.GetWidth() == 0:
        edge = b.GetBoundingBox()
    tb = b.GetTitleBlock()
    by_layer = {}
    for t in tracks:
        by_layer[b.GetLayerName(t.GetLayer())] = by_layer.get(b.GetLayerName(t.GetLayer()), 0) + t.GetLength()
    via_types = {}
    for v in vias:
        k = _VIA_TYPES.get(v.GetViaType(), str(v.GetViaType()))
        via_types[k] = via_types.get(k, 0) + 1
    netclasses = []
    try:
        ns = ds.m_NetSettings
        dn = ns.GetDefaultNetclass()
        ncs = [dn] + list(ns.GetNetclasses().values())
        for nc in ncs:
            netclasses.append({
                "name": str(nc.GetName()),
                "clearance": mm(nc.GetClearance()),
                "track_width": mm(nc.GetTrackWidth()),
                "via_dia": mm(nc.GetViaDiameter()),
                "via_drill": mm(nc.GetViaDrill()),
            })
    except Exception:
        pass
    zones = []
    for z in b.Zones():
        zones.append({
            "net": str(z.GetNetname()),
            "name": str(z.GetZoneName()),
            "layers": ",".join(b.GetLayerName(l) for l in z.GetLayerSet().Seq()),
            "rule_area": bool(z.GetIsRuleArea()),
            "priority": z.GetAssignedPriority(),
            "filled_area_mm2": round(pcbnew.ToMM(pcbnew.ToMM(z.GetFilledArea())), 2) if hasattr(z, "GetFilledArea") else None,
        })
    drawings = {}
    for d in b.GetDrawings():
        k = b.GetLayerName(d.GetLayer())
        drawings[k] = drawings.get(k, 0) + 1
    return {
        "file": os.path.abspath(pcb),
        "title": str(tb.GetTitle()),
        "revision": str(tb.GetRevision()),
        "date": str(tb.GetDate()),
        "outline": bbox_dict(edge),
        "copper_layers": [b.GetLayerName(l) for l in b.GetEnabledLayers().Seq() if pcbnew.IsCopperLayer(l)],
        "other_layers": [b.GetLayerName(l) for l in b.GetEnabledLayers().Seq() if not pcbnew.IsCopperLayer(l)],
        "board_thickness": mm(ds.GetBoardThickness()),
        "footprints": {
            "total": len(fps),
            "front": sum(1 for f in fps if not f.IsFlipped()),
            "back": sum(1 for f in fps if f.IsFlipped()),
            "smd": sum(1 for f in fps if f.GetAttributes() & pcbnew.FP_SMD),
            "tht": sum(1 for f in fps if f.GetAttributes() & pcbnew.FP_THROUGH_HOLE),
            "dnp": sum(1 for f in fps if f.IsDNP()),
        },
        "nets": len(all_nets(b)),
        "tracks": {"count": len(tracks), "total_length_mm": mm(sum(t.GetLength() for t in tracks)),
                   "length_by_layer_mm": {k: mm(v) for k, v in by_layer.items()}},
        "vias": {"count": len(vias), "by_type": via_types},
        "zones": zones,
        "drawings_by_layer": drawings,
        "rules": {
            "min_clearance": mm(ds.m_MinClearance),
            "min_track_width": mm(ds.m_TrackMinWidth),
            "min_via_diameter": mm(ds.m_ViasMinSize),
            "min_through_hole": mm(ds.m_MinThroughDrill),
            "copper_to_edge": mm(ds.m_CopperEdgeClearance),
        },
        "netclasses": netclasses,
    }


def list_components(pcb, ref=None, value=None, footprint=None, side=None, dnp=None, limit=200, offset=0):
    b = load(pcb)
    rows = []
    for f in b.GetFootprints():
        r = fp_row(b, f)
        if not glob_match(r["ref"], ref):
            continue
        if not glob_match(r["value"], value):
            continue
        if not glob_match(r["footprint"], footprint):
            continue
        if side and r["side"] != side.upper()[0]:
            continue
        if dnp is not None and r["dnp"] != bool(dnp):
            continue
        rows.append(r)
    rows.sort(key=lambda r: ref_key(r["ref"]))
    total = len(rows)
    return {"total": total, "rows": rows[offset:offset + limit]}


def get_component(pcb, ref):
    b = load(pcb)
    f = find_fp(b, ref)
    r = fp_row(b, f)
    r["layer"] = str(f.GetLayerName())
    r["exclude_from_bom"] = bool(f.IsExcludedFromBOM())
    r["exclude_from_pos"] = bool(f.IsExcludedFromPosFiles())
    r["bbox"] = bbox_dict(f.GetBoundingBox(False))
    c = fp_courtyard(f)
    r["courtyard"] = bbox_dict(c.BBox()) if c else None
    r["fields"] = {str(fl.GetName()): str(fl.GetText()) for fl in f.GetFields() if str(fl.GetText()).strip()}
    r["description"] = str(f.GetLibDescription())
    r["sheet"] = str(f.GetSheetname())
    r["models"] = [str(m.m_Filename) for m in f.Models()]
    r["pads_detail"] = [pad_dict(b, p) for p in f.Pads()]
    r["pads_detail"].sort(key=lambda p: ref_key(p["pad"]) if p["pad"][:1].isalpha() else ("", int(re.sub(r"\D", "", p["pad"]) or 0), p["pad"]))
    return r


def _parse_item(b, spec):
    """'R12' -> footprint, 'R12.3' / 'R12:3' -> pad, '12.5,30' -> point (mm)."""
    spec = str(spec).strip()
    m = re.match(r"^\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*$", spec)
    if m:
        pt = pcbnew.VECTOR2I(from_mm(m.group(1)), from_mm(m.group(2)))
        return {"kind": "point", "label": "(%s, %s)" % (m.group(1), m.group(2)), "pos": pt, "shape": None, "bbox": None, "point": pt}
    m = re.match(r"^([^.:]+)[.:](.+)$", spec)
    if m:
        f = find_fp(b, m.group(1))
        pad = None
        for p in f.Pads():
            if str(p.GetNumber()) == m.group(2):
                pad = p
                break
        if pad is None:
            raise KeyError("%s has no pad %r" % (m.group(1), m.group(2)))
        return {"kind": "pad", "label": "%s.%s (%s)" % (f.GetReference(), pad.GetNumber(), pad.GetNetname()),
                "pos": pad.GetPosition(), "shape": pad.GetEffectiveShape(pad_copper_layer(pad)),
                "bbox": pad.GetBoundingBox(), "point": None}
    f = find_fp(b, spec)
    c = fp_courtyard(f)
    return {"kind": "footprint", "label": "%s (%s, %s side)" % (f.GetReference(), f.GetValue(), "B" if f.IsFlipped() else "F"),
            "pos": f.GetPosition(), "shape": c or fp_copper_shape(f), "shape_kind": "courtyard" if c else "copper",
            "bbox": f.GetBoundingBox(False), "point": None, "fp": f}


def measure(pcb, a, b_item):
    b = load(pcb)
    A = _parse_item(b, a)
    B = _parse_item(b, b_item)
    d = B["pos"] - A["pos"]
    res = {
        "a": A["label"], "b": B["label"],
        "dx": mm(d.x), "dy": mm(d.y),
        "center_distance": mm(math.hypot(d.x, d.y)),
        "direction_a_to_b": bearing(d.x, d.y),
    }
    if A["shape"] is not None and B["shape"] is not None:
        res["edge_gap"] = shape_gap(A["shape"], B["shape"])
        kinds = {A.get("shape_kind", "copper"), B.get("shape_kind", "copper")}
        res["edge_gap_basis"] = "courtyard" if kinds == {"courtyard"} else ("copper" if kinds == {"copper"} else "mixed courtyard/copper")
    elif A["point"] is not None and B["shape"] is not None:
        res["edge_gap"] = point_gap(B["shape"], A["point"])
        res["edge_gap_basis"] = B.get("shape_kind", "copper")
    elif B["point"] is not None and A["shape"] is not None:
        res["edge_gap"] = point_gap(A["shape"], B["point"])
        res["edge_gap_basis"] = A.get("shape_kind", "copper")
    if A["bbox"] is not None and B["bbox"] is not None:
        res["bbox_gap"] = bbox_gap(A["bbox"], B["bbox"])
    if A["kind"] == "footprint" and B["kind"] == "footprint":
        res["same_side"] = A["fp"].IsFlipped() == B["fp"].IsFlipped()
    return res


def net_info(pcb, net):
    b = load(pcb)
    n = find_net(b, net)
    code = n.GetNetCode()
    name = str(n.GetNetname())
    pads = [pad_dict(b, p) for p in net_pads(b, code)]
    pads.sort(key=lambda p: (ref_key(p["ref"]), p["pad"]))
    tracks = [t for t in b.GetTracks() if t.GetNetCode() == code and t.Type() in (pcbnew.PCB_TRACE_T, pcbnew.PCB_ARC_T)]
    vias = [t for t in b.GetTracks() if t.GetNetCode() == code and t.Type() == pcbnew.PCB_VIA_T]
    by_layer = {}
    widths = set()
    for t in tracks:
        ln = b.GetLayerName(t.GetLayer())
        by_layer[ln] = by_layer.get(ln, 0) + t.GetLength()
        widths.add(mm(t.GetWidth()))
    zones = [str(z.GetZoneName()) or "(unnamed)" for z in b.Zones() if z.GetNetCode() == code]
    bb = None
    for item in list(net_pads(b, code)) + tracks + vias:
        ib = item.GetBoundingBox()
        bb = ib if bb is None else bb.Merge(ib) or bb
    res = {
        "name": name, "code": code,
        "pads": pads,
        "tracks": {"count": len(tracks), "total_length_mm": mm(sum(t.GetLength() for t in tracks)),
                   "by_layer_mm": {k: mm(v) for k, v in by_layer.items()}, "widths_mm": sorted(widths)},
        "vias": {"count": len(vias), "drills_mm": sorted({mm(v.GetDrillValue()) for v in vias})},
        "zones": zones,
        "extent": bbox_dict(bb) if bb is not None else None,
    }
    res.update(netclass_for(b, name))
    return res


def list_nets(pcb, filter=None, limit=100, sort="pads"):
    b = load(pcb)
    tl = {}
    vc = {}
    for t in b.GetTracks():
        c = t.GetNetCode()
        if t.Type() == pcbnew.PCB_VIA_T:
            vc[c] = vc.get(c, 0) + 1
        else:
            tl[c] = tl.get(c, 0) + t.GetLength()
    pc = {}
    for p in b.GetPads():
        pc[p.GetNetCode()] = pc.get(p.GetNetCode(), 0) + 1
    rows = []
    for ni in all_nets(b):
        name = str(ni.GetNetname())
        if not glob_match(name, filter):
            continue
        c = ni.GetNetCode()
        rows.append({"net": name, "pads": pc.get(c, 0), "track_mm": mm(tl.get(c, 0)), "vias": vc.get(c, 0),
                     "netclass": netclass_for(b, name).get("netclass", "")})
    key = {"pads": lambda r: (-r["pads"], r["net"]), "length": lambda r: (-r["track_mm"], r["net"]),
           "name": lambda r: r["net"].lower()}.get(sort, lambda r: (-r["pads"], r["net"]))
    rows.sort(key=key)
    return {"total": len(rows), "rows": rows[:limit]}


def neighbors(pcb, ref, radius=5.0, limit=20):
    b = load(pcb)
    f = find_fp(b, ref)
    fbb = f.GetBoundingBox(False)
    fpos = f.GetPosition()
    fshape = fp_courtyard(f) or fp_copper_shape(f)
    rad = from_mm(radius)
    rows = []
    for g in b.GetFootprints():
        if g is f or str(g.GetReference()) == str(f.GetReference()):
            continue
        gbb = g.GetBoundingBox(False)
        if bbox_gap(fbb, gbb) > radius:
            continue
        d = g.GetPosition() - fpos
        gshape = fp_courtyard(g) or fp_copper_shape(g)
        gap = shape_gap(fshape, gshape, max_mm=radius * 2) if g.IsFlipped() == f.IsFlipped() else None
        rows.append({
            "ref": str(g.GetReference()), "value": str(g.GetValue()), "side": "B" if g.IsFlipped() else "F",
            "dir": bearing(d.x, d.y), "center_mm": mm(math.hypot(d.x, d.y)),
            "gap_mm": gap if gap is not None else bbox_gap(fbb, gbb),
            "gap_basis": ("courtyard" if fp_courtyard(g) and fp_courtyard(f) else "copper") if gap is not None else "bbox (other side)",
        })
    rows.sort(key=lambda r: (r["gap_mm"], r["center_mm"]))
    return {"ref": str(f.GetReference()), "side": "B" if f.IsFlipped() else "F", "pos": {"x": mm(fpos.x), "y": mm(fpos.y)},
            "total": len(rows), "rows": rows[:limit]}


def items_in_region(pcb, x1, y1, x2, y2, layer=None):
    b = load(pcb)
    box = pcbnew.BOX2I(pcbnew.VECTOR2I(from_mm(min(x1, x2)), from_mm(min(y1, y2))),
                       pcbnew.VECTOR2I(from_mm(abs(x2 - x1)), from_mm(abs(y2 - y1))))
    lid = None
    if layer:
        lid = b.GetLayerID(layer)
        if lid < 0:
            raise KeyError("Unknown layer %r" % layer)
    fps = []
    for f in b.GetFootprints():
        if lid is not None and f.GetLayer() != lid:
            continue
        if box.Intersects(f.GetBoundingBox(False)):
            r = fp_row(b, f)
            fps.append({k: r[k] for k in ("ref", "value", "x", "y", "side")})
    fps.sort(key=lambda r: ref_key(r["ref"]))
    tracks = {}
    vias = 0
    for t in b.GetTracks():
        if not box.Intersects(t.GetBoundingBox()):
            continue
        if t.Type() == pcbnew.PCB_VIA_T:
            if lid is None or t.IsOnLayer(lid):
                vias += 1
            continue
        if lid is not None and t.GetLayer() != lid:
            continue
        k = (str(t.GetNetname()), b.GetLayerName(t.GetLayer()))
        e = tracks.setdefault(k, {"net": k[0], "layer": k[1], "segments": 0, "length_mm": 0})
        e["segments"] += 1
        e["length_mm"] = mm(from_mm(e["length_mm"]) + t.GetLength())
    zones = []
    for z in b.Zones():
        if box.Intersects(z.GetBoundingBox()):
            if lid is None or z.IsOnLayer(lid):
                zones.append("%s [%s]" % (z.GetNetname() or z.GetZoneName() or "rule area",
                                          ",".join(b.GetLayerName(l) for l in z.GetLayerSet().Seq())))
    texts = []
    drawings = {}
    for d in b.GetDrawings():
        if not box.Intersects(d.GetBoundingBox()):
            continue
        if lid is not None and d.GetLayer() != lid:
            continue
        if d.Type() == pcbnew.PCB_TEXT_T:
            texts.append({"text": str(d.GetText()).strip(), "layer": b.GetLayerName(d.GetLayer())})
        else:
            k = b.GetLayerName(d.GetLayer())
            drawings[k] = drawings.get(k, 0) + 1
    return {"region": {"x": min(x1, x2), "y": min(y1, y2), "w": abs(x2 - x1), "h": abs(y2 - y1)}, "layer": layer,
            "footprints": fps, "tracks": sorted(tracks.values(), key=lambda e: -e["length_mm"]),
            "vias": vias, "zones": zones, "texts": texts, "drawings_by_layer": drawings}


def search(pcb, query, limit=50):
    b = load(pcb)
    q = query.lower()
    hits = []
    for f in b.GetFootprints():
        fields = {str(fl.GetName()): str(fl.GetText()) for fl in f.GetFields()}
        where = []
        if q in str(f.GetReference()).lower():
            where.append("ref")
        if q in str(f.GetValue()).lower():
            where.append("value")
        if q in str(f.GetFPIDAsString()).lower():
            where.append("footprint")
        for k, v in fields.items():
            if k not in ("Reference", "Value") and q in v.lower():
                where.append("field " + k)
        if where:
            p = f.GetPosition()
            hits.append({"kind": "footprint", "item": "%s %s" % (f.GetReference(), f.GetValue()), "match": ", ".join(where),
                         "x": mm(p.x), "y": mm(p.y), "side": "B" if f.IsFlipped() else "F"})
    for ni in all_nets(b):
        if q in str(ni.GetNetname()).lower():
            hits.append({"kind": "net", "item": str(ni.GetNetname()), "match": "name"})
    for d in b.GetDrawings():
        if d.Type() == pcbnew.PCB_TEXT_T and q in str(d.GetText()).lower():
            p = d.GetPosition()
            hits.append({"kind": "text", "item": str(d.GetText()).strip(), "match": b.GetLayerName(d.GetLayer()), "x": mm(p.x), "y": mm(p.y)})
    return {"total": len(hits), "rows": hits[:limit]}


def layers(pcb):
    b = load(pcb)
    return [{"id": l, "name": b.GetLayerName(l), "copper": bool(pcbnew.IsCopperLayer(l))} for l in b.GetEnabledLayers().Seq()]


def ping():
    return {"pcbnew": pcbnew.GetBuildVersion(), "python": sys.version.split()[0]}


METHODS = {
    "ping": ping,
    "board_summary": board_summary,
    "list_components": list_components,
    "get_component": get_component,
    "measure": measure,
    "net_info": net_info,
    "list_nets": list_nets,
    "neighbors": neighbors,
    "items_in_region": items_in_region,
    "search": search,
    "layers": layers,
}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            fn = METHODS[req["method"]]
            res = fn(**req.get("params", {}))
            out = {"id": req.get("id"), "result": res}
        except Exception as e:  # noqa: BLE001
            msg = e.args[0] if isinstance(e, (KeyError, FileNotFoundError)) and e.args else "%s: %s" % (type(e).__name__, e)
            out = {"id": (req.get("id") if isinstance(req, dict) else None),
                   "error": str(msg), "trace": traceback.format_exc()}
        _out.write(json.dumps(out, default=str) + "\n")
        _out.flush()


if __name__ == "__main__":
    main()
