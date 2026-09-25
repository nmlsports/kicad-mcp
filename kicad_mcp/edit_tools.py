"""MCP tools that edit the board live through KiCad's IPC API."""
import functools
import json
from typing import Optional, Union

from mcp.types import ToolAnnotations

from . import ipc
from .fmt import kv, table


def register(mcp) -> dict:
    """Register the editing tools on `mcp`; returns {name: function} for direct use/tests."""
    def edit_tool(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except (ipc.IpcError, FileNotFoundError, ValueError) as e:
                return f"Error: {e}"
            except Exception as e:  # kipy / pynng errors
                return f"Error: {type(e).__name__}: {e}"
        return mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))(wrapper)

    def _list(arg, what):
        if isinstance(arg, str):
            try:
                arg = json.loads(arg)
            except json.JSONDecodeError as e:
                raise ValueError(f"{what} must be JSON: {e}")
        if not isinstance(arg, list):
            raise ValueError(f"{what} must be a list")
        return arg

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def kicad_status(path: Optional[str] = None) -> str:
        """Is KiCad running with its API enabled, and which boards/schematics are open? Call before
        any editing tool. Editing tools work on the board open in the KiCad PCB editor."""
        try:
            s = ipc.status(path)
        except ipc.IpcError as e:
            return f"Not connected. {e}"
        return kv({"KiCad": s["kicad_version"], "instances": s["instances"],
                   "open boards": ", ".join(s["open_boards"]) or "none", "note": s.get("note"),
                   "open schematics": ", ".join(map(str, s["open_schematics"])) or "none",
                   "requested board open": s.get("requested_board_open")})

    @edit_tool
    def move_component(path: Optional[str] = None, ref: str = "", x: Optional[float] = None,
                       y: Optional[float] = None, dx: float = 0, dy: float = 0,
                       rotation: Optional[float] = None, rotate_by: float = 0, flip: bool = False) -> str:
        """Move/rotate/flip one footprint in the open board (one undo step). x/y set an absolute
        position in mm; dx/dy nudge relative to it; rotation sets degrees, rotate_by adds; flip
        moves it to the other side."""
        rows = ipc.move_footprints(ipc.open_board(path), [{"ref": ref, "x": x, "y": y, "dx": dx, "dy": dy,
                                                            "rotation": rotation, "rotate_by": rotate_by, "flip": flip}])
        return table(rows, ["ref", "value", "x", "y", "rot", "side", "locked"]) + "\n" + ipc.UNSAVED_NOTE

    @edit_tool
    def move_components(path: Optional[str] = None, moves: Union[list, str] = "") -> str:
        """Move several footprints as one undo step. `moves` is a JSON list of objects with keys
        ref (required) and any of x, y, dx, dy, rotation, rotate_by, flip, locked. Example:
        [{"ref":"C1","x":120.5,"y":44,"rotation":90},{"ref":"R2","dx":-1.27}]"""
        moves = _list(moves, "moves")
        rows = ipc.move_footprints(ipc.open_board(path), moves)
        return table(rows, ["ref", "value", "x", "y", "rot", "side", "locked"]) + "\n" + ipc.UNSAVED_NOTE

    @edit_tool
    def set_component_attributes(path: Optional[str] = None, ref: str = "", dnp: Optional[bool] = None,
                                 exclude_from_bom: Optional[bool] = None, exclude_from_pos: Optional[bool] = None,
                                 locked: Optional[bool] = None) -> str:
        """Set DNP / exclude-from-BOM / exclude-from-position-files / locked flags on a footprint."""
        r = ipc.set_attributes(ipc.open_board(path), ref, dnp, exclude_from_bom, exclude_from_pos, locked)
        return kv(r) + "\n" + ipc.UNSAVED_NOTE

    @edit_tool
    def set_component_field(path: Optional[str] = None, ref: str = "", field: str = "value", value: str = "") -> str:
        """Set the value, datasheet or description text of a footprint on the board (not the schematic)."""
        r = ipc.set_field(ipc.open_board(path), ref, field, value)
        return kv(r) + "\n" + ipc.UNSAVED_NOTE

    @edit_tool
    def add_track(path: Optional[str] = None, net: str = "", points: Union[list, str] = "",
                  layer: str = "F.Cu", width: Optional[float] = None) -> str:
        """Draw track segments through `points` (JSON list of [x, y] in mm) on `layer` for `net`.
        Width defaults to the net's netclass. No DRC is run; check with drc after saving."""
        pts = _list(points, "points")
        r = ipc.add_tracks(ipc.open_board(path), net, pts, layer, width)
        return kv(r) + "\n" + ipc.UNSAVED_NOTE

    @edit_tool
    def add_via(path: Optional[str] = None, net: str = "", x: float = 0, y: float = 0,
                diameter: Optional[float] = None, drill: Optional[float] = None) -> str:
        """Place a through via at (x, y) mm on `net`. Diameter/drill default to the netclass."""
        r = ipc.add_via(ipc.open_board(path), net, x, y, diameter, drill)
        return kv(r) + "\n" + ipc.UNSAVED_NOTE

    @edit_tool
    def ripup_net(path: Optional[str] = None, net: str = "", layer: Optional[str] = None, vias: bool = True) -> str:
        """Delete all tracks (and vias unless vias=false) of a net, optionally only on one layer.
        Undoable in the editor."""
        r = ipc.ripup_net(ipc.open_board(path), net, layer, vias)
        return kv(r) + "\n" + ipc.UNSAVED_NOTE

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_selection(path: Optional[str] = None) -> str:
        """What the user currently has selected in the PCB editor (footprints, tracks, vias...).
        Useful when they say "this part" or "these tracks"."""
        try:
            items = ipc.get_selection(ipc.open_board(path))
        except Exception as e:
            return f"Error: {e}"
        return "\n".join(f"- {i}" for i in items) if items else "(nothing selected)"

    @edit_tool
    def select_components(path: Optional[str] = None, refs: Union[list, str] = "", add: bool = False) -> str:
        """Select footprints in the PCB editor so the user can see which ones you mean. `refs` is a
        JSON list of references. add=true keeps the existing selection."""
        refs = [str(x) for x in _list(refs, "refs")]
        r = ipc.select(ipc.open_board(path), refs, clear=not add)
        return "Selected: " + ", ".join(r)

    @edit_tool
    def refill_zones(path: Optional[str] = None) -> str:
        """Refill all copper zones in the open board (needed after moving parts or routing near zones)."""
        ipc.refill_zones(ipc.open_board(path))
        return "Zones refilled.\n" + ipc.UNSAVED_NOTE

    @edit_tool
    def save_board(path: Optional[str] = None) -> str:
        """Save the open board to disk so file-based tools (measure, drc, render...) see the edits."""
        return f"Saved {ipc.save(ipc.open_board(path))}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def live_component(path: Optional[str] = None, ref: str = "") -> str:
        """Current position/rotation/side of a footprint as shown in the editor (including unsaved edits)."""
        try:
            return kv(ipc.live_footprint(ipc.open_board(path), ref))
        except Exception as e:
            return f"Error: {e}"

    return {f.__name__: f for f in (kicad_status, move_component, move_components, set_component_attributes,
                                    set_component_field, add_track, add_via, ripup_net, get_selection,
                                    select_components, refill_zones, save_board, live_component)}
