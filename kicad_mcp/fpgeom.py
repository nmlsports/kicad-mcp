"""Rigid moves of board footprints for KiCad's IPC API, done on the protobuf messages.

KiCad's UpdateItems rebuilds a footprint from the message it receives, and footprint children
travel in absolute board coordinates, so the client has to move every child itself. kipy's
FootprintInstance.position / .orientation setters (kicad-python 0.8.0) get this wrong:
- the orientation setter rebuilds definition.items from pads, fields, texts, zones and shapes only,
  so 3D models (and text boxes, dimensions, barcodes, images) are dropped from the footprint;
- neither setter moves text boxes, dimensions, barcodes or reference images;
- Vector2.rotate and from_mm truncate with int(), so a quarter turn leaves children 1 nm off
  (cos 90deg is 6e-17, not 0) and DRC reports lib_footprint_mismatch;
- the rotation delta is not normalized, so texts end up at angles like -270.

place() replaces both setters: it keeps every item and follows KiCad's RotatePoint (exact for
multiples of 90 degrees, KiROUND otherwise), normalizing angles to the ranges KiCad writes.
"""
from __future__ import annotations

import math

from kipy.board_types import FootprintInstance
from kipy.proto.board import board_types_pb2 as bt


class Unsupported(ValueError):
    pass


def kiround(v: float) -> int:
    """KiCad's KiROUND: round half away from zero."""
    return int(v + 0.5) if v >= 0 else int(v - 0.5)


def nm(mm: float) -> int:
    """mm -> nm, rounded (kipy's from_mm truncates: 4.35 mm -> 4349999 nm)."""
    return kiround(float(mm) * 1_000_000)


def _clean(deg: float) -> float:
    d = round(float(deg), 9)
    return 0.0 if d == 0 else d


def norm360(deg: float) -> float:
    """[0, 360): how KiCad stores pad and text angles."""
    return _clean(_clean(deg) % 360.0)


def norm180(deg: float) -> float:
    """(-180, 180]: how KiCad stores footprint orientation."""
    d = norm360(deg)
    return d - 360.0 if d > 180.0 else d


def rotate(x: int, y: int, deg: float) -> tuple[int, int]:
    """KiCad's RotatePoint about the origin (positive = counter-clockwise on screen)."""
    a = norm360(deg)
    if a == 0:
        return x, y
    if a == 90:
        return y, -x
    if a == 180:
        return -x, -y
    if a == 270:
        return -y, x
    r = math.radians(a)
    s, c = math.sin(r), math.cos(r)
    return kiround(y * s + x * c), kiround(y * c - x * s)


class _Transform:
    """Maps board coordinates of a footprint at (old_pos, old_deg) to (new_pos, new_deg)."""

    def __init__(self, old_pos: tuple[int, int], old_deg: float, new_pos: tuple[int, int], new_deg: float):
        self.old_pos, self.old_deg = old_pos, old_deg
        self.new_pos, self.new_deg = new_pos, new_deg
        self.delta = norm360(new_deg - old_deg)
        self.cardinal = self.delta % 90 == 0

    def point(self, v) -> None:
        x, y = v.x_nm - self.old_pos[0], v.y_nm - self.old_pos[1]
        if self.cardinal:
            x, y = rotate(x, y, self.delta)  # exact
        else:
            # through footprint-local coordinates, so that a part turned back to a multiple of 90
            # degrees gets its library geometry back (to within KiCad's own rounding)
            x, y = rotate(*rotate(x, y, -self.old_deg), self.new_deg)
        v.x_nm, v.y_nm = x + self.new_pos[0], y + self.new_pos[1]

    def angle(self, a) -> None:
        a.value_degrees = norm360(a.value_degrees + self.delta)

    def text(self, t) -> None:  # kiapi.common.types.Text
        self.point(t.position)
        self.angle(t.attributes.angle)

    def polyset(self, ps) -> None:
        for poly in ps.polygons:
            for line in [poly.outline, *poly.holes]:
                for node in line.nodes:
                    if node.HasField("arc"):
                        for p in (node.arc.start, node.arc.mid, node.arc.end):
                            self.point(p)
                    else:
                        self.point(node.point)

    def shape(self, gs) -> None:  # kiapi.common.types.GraphicShape
        kind = gs.WhichOneof("geometry")
        if kind is None:
            return
        g = getattr(gs, kind)
        if kind == "segment":
            pts = (g.start, g.end)
        elif kind == "arc":
            pts = (g.start, g.mid, g.end)
        elif kind == "circle":
            pts = (g.center, g.radius_point)
        elif kind == "bezier":
            pts = (g.start, g.control1, g.control2, g.end)
        elif kind == "polygon":
            self.polyset(g)
            return
        elif kind == "rectangle":
            if not self.cardinal:
                # like KiCad, a rectangle turned off-axis becomes a polygon
                (x1, y1), (x2, y2) = (g.top_left.x_nm, g.top_left.y_nm), (g.bottom_right.x_nm, g.bottom_right.y_nm)
                gs.ClearField("rectangle")
                line = gs.polygon.polygons.add().outline
                line.closed = True
                for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
                    n = line.nodes.add()
                    n.point.x_nm, n.point.y_nm = x, y
                self.polyset(gs.polygon)
                return
            # rotate the corners as points (no min/max normalisation), as KiCad does, so the
            # footprint-local start/end written to the file stay those of the library
            pts = (g.top_left, g.bottom_right)
        else:
            raise Unsupported(f"graphic shape of unknown kind {kind!r}")
        for p in pts:
            self.point(p)

    def item(self, msg, what: str) -> None:
        if isinstance(msg, bt.Pad):
            self.point(msg.position)
            self.angle(msg.pad_stack.angle)
        elif isinstance(msg, bt.Field):
            self.text(msg.text.text)
        elif isinstance(msg, bt.BoardText):
            self.text(msg.text)
        elif isinstance(msg, bt.BoardGraphicShape):
            self.shape(msg.shape)
        elif isinstance(msg, bt.Zone):
            self.polyset(msg.outline)
            for layer in msg.filled_polygons:
                self.polyset(layer.shapes)
        elif isinstance(msg, bt.BoardTextBox):
            if not self.cardinal:
                raise Unsupported(f"{what} has a text box, which KiCad's API cannot rotate by "
                                  f"{self.delta:g} degrees (only multiples of 90)")
            self.point(msg.textbox.top_left)
            self.point(msg.textbox.bottom_right)
            self.angle(msg.textbox.attributes.angle)
        elif isinstance(msg, bt.Dimension):
            style = msg.WhichOneof("dimension_style")
            if style == "orthogonal" and self.delta:
                raise Unsupported(f"{what} has an orthogonal dimension; rotate it in KiCad instead")
            d = getattr(msg, style) if style else None
            for name in ("start", "end", "center", "radius_point"):
                if d is not None and name in d.DESCRIPTOR.fields_by_name:
                    self.point(getattr(d, name))
            self.text(msg.text)
        elif isinstance(msg, bt.Barcode):
            self.point(msg.position)
            self.angle(msg.orientation)
        elif isinstance(msg, bt.ReferenceImage):
            if self.delta:
                raise Unsupported(f"{what} has a reference image, which KiCad's API cannot rotate")
            self.point(msg.position)
        elif isinstance(msg, (bt.Footprint3DModel, bt.Group)):
            pass  # footprint-relative / membership only
        else:
            raise Unsupported(f"{what} contains a {type(msg).__name__}, which kicad-mcp does not know how to move")


def place(fp: FootprintInstance, x_nm: int, y_nm: int, orientation_deg: float) -> bool:
    """Move `fp` to (x_nm, y_nm) at `orientation_deg`, carrying every child along, in place.
    Returns False (and changes nothing) when it is already there. Raises Unsupported, leaving
    `fp` untouched, if a child cannot be moved that way."""
    p = fp.proto
    old_pos = (p.position.x_nm, p.position.y_nm)
    old_deg = p.orientation.value_degrees
    new_pos, new_deg = (int(x_nm), int(y_nm)), norm180(orientation_deg)
    if new_pos == old_pos and norm360(new_deg - old_deg) == 0:
        return False
    xf = _Transform(old_pos, old_deg, new_pos, new_deg)
    what = f"footprint {p.reference_field.text.text.text or p.id.value}"
    d = p.definition
    fields = (p.reference_field, p.value_field, p.datasheet_field, p.description_field,
              d.reference_field, d.value_field, d.datasheet_field, d.description_field)
    texts = [f.text.text for f in fields if f.HasField("text") and f.text.HasField("text")]
    items = [i.proto for i in fp.definition.items]
    # work on copies so that an Unsupported child leaves the footprint as it was
    moved_texts, moved_items = [_copy(t) for t in texts], [_copy(m) for m in items]
    for t in moved_texts:
        xf.text(t)
    for m in moved_items:
        xf.item(m, what)
    for orig, new in zip(texts + items, moved_texts + moved_items):
        orig.CopyFrom(new)
    p.position.x_nm, p.position.y_nm = new_pos
    p.orientation.value_degrees = new_deg
    return True


def _copy(msg):
    m = type(msg)()
    m.CopyFrom(msg)
    return m
