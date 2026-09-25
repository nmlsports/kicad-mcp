"""Live editing through KiCad's IPC API (kicad-python / kipy). Needs a running KiCad with the
API server enabled (Preferences > Plugins > "Enable KiCad API") and the board open in the PCB editor.

Every edit is wrapped in one commit so it shows up as a single undo step in the editor.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Optional, Sequence

from kipy import KiCad
from kipy.board import Board
from kipy.board_types import FootprintInstance, Net, Track, Via
from kipy.errors import ApiError
from kipy.errors import ConnectionError as KiCadConnectionError
from kipy.geometry import Angle, Vector2
from kipy.proto.common.types.base_types_pb2 import DocumentType
from kipy.util.board_layer import canonical_name, layer_from_canonical_name
from kipy.util.units import from_mm, to_mm

from .paths import resolve_pcb

ENABLE_HINT = (
    "KiCad's API server is not reachable. Make sure KiCad is running, the board is open in the PCB "
    "editor, and the API is on: Preferences > Preferences... > Plugins > tick 'Enable KiCad API' "
    "(restart KiCad after enabling). Socket: " + (os.environ.get("KICAD_API_SOCKET") or "default")
)

UNSAVED_NOTE = ("Change applied in the editor (one undo step) but not saved to disk. "
                "Call save_board before using file-based tools (measure, drc, render...).")


class IpcError(RuntimeError):
    pass


def socket_candidates() -> list[str]:
    """KICAD_API_SOCKET, then KiCad's default socket, then per-process sockets (standalone editors)."""
    cands = []
    env = os.environ.get("KICAD_API_SOCKET")
    if env:
        cands.append(env)
    try:
        from kipy.kicad import _default_socket_path
        cands.append(_default_socket_path())
    except Exception:
        cands.append("ipc:///tmp/kicad/api.sock")
    sock_dir = Path("/tmp/kicad")
    if sock_dir.is_dir():
        extra = sorted(sock_dir.glob("api-*.sock"), key=lambda q: q.stat().st_mtime, reverse=True)
        cands.extend("ipc://" + str(q) for q in extra)
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def connections(timeout_ms: int = 5000) -> list[KiCad]:
    """Every reachable KiCad API endpoint."""
    found, errors = [], []
    for sock in socket_candidates():
        try:
            k = KiCad(socket_path=sock, client_name="kicad-mcp", timeout_ms=timeout_ms)
            k.ping()
            found.append(k)
        except Exception as e:  # pynng raises its own types
            errors.append(f"{sock}: {type(e).__name__}: {e}")
    if not found:
        raise IpcError(ENABLE_HINT + "\nTried: " + "; ".join(errors))
    return found


def connect(timeout_ms: int = 5000) -> KiCad:
    return connections(timeout_ms)[0]


def _doc_path(doc) -> Path:
    p = Path(doc.board_filename)
    if not p.is_absolute() and doc.HasField("project") and doc.project.path:
        p = Path(doc.project.path) / p
    return p


NO_EDITOR_HINT = ("KiCad is running with the API on, but no PCB editor window has registered with it. "
                  "Open the board in the PCB editor (double-click the .kicad_pcb in the project manager).")


def open_documents(k: KiCad, doc_type) -> list:
    try:
        return list(k.get_open_documents(doc_type))
    except ApiError as e:
        if "no handler" in str(e):
            raise IpcError(NO_EDITOR_HINT)
        raise IpcError(str(e))


def open_board(path: Optional[str]) -> Board:
    """The Board for the open document matching `path` (or the only open board), searching every
    reachable KiCad instance (project-manager session and standalone editors)."""
    want: Optional[Path] = None
    if path:
        try:
            want = resolve_pcb(path).resolve()
        except FileNotFoundError:
            want = Path(os.path.expanduser(path)).resolve()
    open_names: list[str] = []
    problems: list[str] = []
    fallback: Optional[Board] = None
    for k in connections():
        try:
            docs = open_documents(k, DocumentType.DOCTYPE_PCB)
        except IpcError as e:
            problems.append(str(e))
            continue
        for d in docs:
            dp = _doc_path(d)
            if str(dp) in open_names:  # same board reported by another instance
                continue
            open_names.append(str(dp))
            if want is None:
                if fallback is None:
                    fallback = Board(k._client, d)
                else:
                    raise IpcError("Several boards are open; pass `path` to pick one: " + ", ".join(open_names))
            elif dp.resolve() == want or (not dp.is_absolute() and dp.name == want.name):
                return Board(k._client, d)
    if want is None and fallback is not None:
        return fallback
    if open_names:
        raise IpcError(f"{want.name if want else 'board'} is not open in KiCad (open boards: {', '.join(open_names)}).")
    raise IpcError(problems[0] if problems else "No board is open in KiCad's PCB editor.")


# ------------------------------------------------------------------ helpers


def ref_of(fp: FootprintInstance) -> str:
    return fp.reference_field.text.value


def side_of(fp: FootprintInstance) -> str:
    return "B" if canonical_name(fp.layer).startswith("B.") else "F"


def fp_state(fp: FootprintInstance) -> dict:
    return {
        "ref": ref_of(fp),
        "value": fp.value_field.text.value,
        "x": round(to_mm(fp.position.x), 4),
        "y": round(to_mm(fp.position.y), 4),
        "rot": round(fp.orientation.degrees, 2),
        "side": side_of(fp),
        "locked": fp.locked,
    }


def footprints_by_ref(board: Board) -> dict[str, FootprintInstance]:
    return {ref_of(f): f for f in board.get_footprints()}


def get_fp(fps: dict[str, FootprintInstance], ref: str) -> FootprintInstance:
    f = fps.get(ref)
    if f is None:
        for k, v in fps.items():
            if k.lower() == ref.lower():
                return v
        raise IpcError(f"No footprint {ref!r} on the open board")
    return f


def find_net(board: Board, name: str) -> Net:
    nets = board.get_nets()
    for n in nets:
        if n.name == name:
            return n
    lname = name.lower()
    exact = [n for n in nets if n.name.lower() == lname]
    if len(exact) == 1:
        return exact[0]
    tail = [n for n in nets if n.name.lower().rsplit("/", 1)[-1] == lname]
    if tail:
        return sorted(tail, key=lambda n: (n.name.count("/"), n.name))[0]
    sub = [n for n in nets if lname in n.name.lower()]
    if len(sub) == 1:
        return sub[0]
    raise IpcError(f"No net matching {name!r}" + (": " + ", ".join(n.name for n in sub[:10]) if sub else ""))


def layer_id(name: str):
    try:
        return layer_from_canonical_name(name)
    except Exception:
        raise IpcError(f"Unknown layer {name!r} (use canonical names like F.Cu, In1.Cu, B.Cu)")


def _commit(board: Board, message: str, fn):
    commit = board.begin_commit()
    try:
        result = fn()
    except Exception:
        board.drop_commit(commit)
        raise
    board.push_commit(commit, message)
    return result


# ------------------------------------------------------------------ operations


def status(path: Optional[str]) -> dict:
    ks = connections()
    pcbs, schs, notes = [], [], []
    for k in ks:
        try:
            pcbs += [str(_doc_path(d)) for d in open_documents(k, DocumentType.DOCTYPE_PCB)
                     if str(_doc_path(d)) not in pcbs]
        except IpcError as e:
            notes.append(str(e))
        try:
            schs += [d.board_filename or d.sheet_path.path_human_readable
                     for d in k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)]
        except Exception:
            pass
    out: dict[str, Any] = {"kicad_version": str(ks[0].get_version()), "instances": len(ks),
                           "open_boards": pcbs, "open_schematics": schs, "note": notes[0] if notes and not pcbs else None}
    if path:
        try:
            want = resolve_pcb(path).resolve()
            out["requested_board_open"] = any(Path(p).resolve() == want or Path(p).name == want.name for p in pcbs)
        except FileNotFoundError as e:
            out["requested_board_open"] = f"error: {e}"
    return out


def move_footprints(board: Board, moves: Sequence[dict]) -> list[dict]:
    """moves: [{ref, x?, y?, dx?, dy?, rotation?, rotate_by?, flip?, locked?}] in mm / degrees.

    Flips run first inside the commit and the moves are computed from the flipped items, because
    changes staged in an open commit are not visible to later calls until it is pushed."""
    fps = footprints_by_ref(board)
    entries: list[tuple[dict, FootprintInstance]] = []
    for m in moves:
        if "ref" not in m:
            raise IpcError(f"move entry without 'ref': {m}")
        entries.append((m, get_fp(fps, str(m["ref"]))))

    def do():
        items = entries
        to_flip = [f for m, f in items if m.get("flip")]
        if to_flip:
            flipped = {str(x.id): x for x in board.flip_items(to_flip)}
            items = [(m, flipped.get(str(f.id), f)) for m, f in items]
        for m, f in items:
            x = m["x"] if m.get("x") is not None else to_mm(f.position.x)
            y = m["y"] if m.get("y") is not None else to_mm(f.position.y)
            x += float(m.get("dx") or 0)
            y += float(m.get("dy") or 0)
            f.position = Vector2.from_xy_mm(float(x), float(y))
            if m.get("rotation") is not None:
                f.orientation = Angle.from_degrees(float(m["rotation"]))
            if m.get("rotate_by"):
                f.orientation = Angle.from_degrees((f.orientation.degrees + float(m["rotate_by"])) % 360)
            if m.get("locked") is not None:
                f.locked = bool(m["locked"])
        return board.update_items([f for _, f in items])

    updated = _commit(board, f"kicad-mcp: move {len(entries)} footprint(s)", do)
    return [fp_state(u) for u in updated]


def set_attributes(board: Board, ref: str, dnp=None, exclude_from_bom=None, exclude_from_pos=None, locked=None) -> dict:
    f = get_fp(footprints_by_ref(board), ref)
    a = f.attributes
    if dnp is not None:
        a.do_not_populate = bool(dnp)
    if exclude_from_bom is not None:
        a.exclude_from_bill_of_materials = bool(exclude_from_bom)
    if exclude_from_pos is not None:
        a.exclude_from_position_files = bool(exclude_from_pos)
    if locked is not None:
        f.locked = bool(locked)
    u = _commit(board, f"kicad-mcp: attributes of {ref}", lambda: board.update_items([f]))[0]
    return {**fp_state(u), "dnp": u.attributes.do_not_populate,
            "exclude_from_bom": u.attributes.exclude_from_bill_of_materials,
            "exclude_from_pos": u.attributes.exclude_from_position_files}


def set_field(board: Board, ref: str, field: str, value: str) -> dict:
    f = get_fp(footprints_by_ref(board), ref)
    key = field.lower()
    getter = {"value": "value_field", "datasheet": "datasheet_field", "description": "description_field"}.get(key)
    if not getter:
        raise IpcError("field must be one of: value, datasheet, description")
    fld = getattr(f, getter)
    txt = fld.text
    txt.value = value
    fld.text = txt
    setattr(f, getter, fld)
    u = _commit(board, f"kicad-mcp: set {field} of {ref}", lambda: board.update_items([f]))[0]
    return {"ref": ref_of(u), field: getattr(u, getter).text.value}


def add_tracks(board: Board, net_name: str, points: Sequence[Sequence[float]], layer: str,
               width_mm: Optional[float] = None) -> dict:
    if len(points) < 2:
        raise IpcError("need at least two points")
    net = find_net(board, net_name)
    lid = layer_id(layer)
    if width_mm is None:
        nc = board.get_netclass_for_nets(net).get(net.name)
        width = (nc.track_width if nc and nc.track_width else None) or from_mm(0.2)
    else:
        width = from_mm(width_mm)
    tracks = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        t = Track()
        t.start = Vector2.from_xy_mm(float(x1), float(y1))
        t.end = Vector2.from_xy_mm(float(x2), float(y2))
        t.layer = lid
        t.width = width
        t.net = net
        tracks.append(t)
    created = _commit(board, f"kicad-mcp: route {net.name}", lambda: board.create_items(tracks))
    length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))
    return {"net": net.name, "layer": layer, "segments": len(created), "width_mm": round(to_mm(width), 4),
            "length_mm": round(length, 4)}


def add_via(board: Board, net_name: str, x: float, y: float, diameter_mm: Optional[float] = None,
            drill_mm: Optional[float] = None) -> dict:
    net = find_net(board, net_name)
    nc = board.get_netclass_for_nets(net).get(net.name)
    v = Via()
    v.position = Vector2.from_xy_mm(float(x), float(y))
    v.net = net
    v.diameter = from_mm(diameter_mm) if diameter_mm else ((nc.via_diameter if nc else None) or from_mm(0.6))
    v.drill_diameter = from_mm(drill_mm) if drill_mm else ((nc.via_drill if nc else None) or from_mm(0.3))
    created = _commit(board, f"kicad-mcp: via on {net.name}", lambda: board.create_items([v]))[0]
    return {"net": net.name, "x": x, "y": y, "diameter_mm": round(to_mm(created.diameter), 4),
            "drill_mm": round(to_mm(created.drill_diameter), 4)}


def ripup_net(board: Board, net_name: str, layer: Optional[str] = None, vias: bool = True) -> dict:
    net = find_net(board, net_name)
    lid = layer_id(layer) if layer else None
    tracks = [t for t in board.get_tracks() if t.net.name == net.name and (lid is None or t.layer == lid)]
    vs = [v for v in board.get_vias() if v.net.name == net.name] if (vias and lid is None) else []
    items = [*tracks, *vs]
    if items:
        _commit(board, f"kicad-mcp: rip up {net.name}", lambda: board.remove_items(items))
    return {"net": net.name, "tracks_removed": len(tracks), "vias_removed": len(vs)}


def describe_item(item) -> str:
    if isinstance(item, FootprintInstance):
        s = fp_state(item)
        return f"footprint {s['ref']} ({s['value']}) at ({s['x']}, {s['y']}) {s['side']}"
    if isinstance(item, Track):
        return (f"track {item.net.name} on {canonical_name(item.layer)} "
                f"({to_mm(item.start.x):.3f},{to_mm(item.start.y):.3f})-({to_mm(item.end.x):.3f},{to_mm(item.end.y):.3f}) "
                f"w={to_mm(item.width):.3f}")
    if isinstance(item, Via):
        return f"via {item.net.name} at ({to_mm(item.position.x):.3f},{to_mm(item.position.y):.3f})"
    name = type(item).__name__
    pos = getattr(item, "position", None)
    where = f" at ({to_mm(pos.x):.3f},{to_mm(pos.y):.3f})" if pos is not None else ""
    return f"{name}{where}"


def get_selection(board: Board) -> list[str]:
    return [describe_item(i) for i in board.get_selection()]


def select(board: Board, refs: Sequence[str], clear: bool = True) -> list[str]:
    fps = footprints_by_ref(board)
    items = [get_fp(fps, r) for r in refs]
    if clear:
        board.clear_selection()
    board.add_to_selection(items)
    return [ref_of(f) for f in items]


def save(board: Board) -> str:
    board.save()
    return str(_doc_path(board.document))


def refill_zones(board: Board) -> None:
    board.refill_zones()


def live_footprint(board: Board, ref: str) -> dict:
    return fp_state(get_fp(footprints_by_ref(board), ref))
