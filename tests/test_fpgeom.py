"""Footprint moves done by kicad_mcp.fpgeom on synthetic API messages; no KiCad needed."""
import pytest
from kipy.board_types import FootprintInstance
from kipy.proto.board import board_types_pb2 as bt

from kicad_mcp import fpgeom
from kicad_mcp.fpgeom import nm

MM = 1_000_000


def _pt(v, x, y):
    v.x_nm, v.y_nm = x, y


def make_fp(x=10 * MM, y=20 * MM, rot=0.0, textbox=False) -> FootprintInstance:
    """A footprint at (x, y, rot) whose children are placed from footprint-local coordinates the
    way KiCad would (exact for multiples of 90 degrees)."""
    p = bt.FootprintInstance()
    p.id.value = "fp-1"
    _pt(p.position, x, y)
    p.orientation.value_degrees = rot

    def at(lx, ly):
        rx, ry = fpgeom.rotate(lx, ly, rot)
        return x + rx, y + ry

    def add(msg):
        p.definition.items.add().Pack(msg)

    p.reference_field.text.text.text = "R1"
    _pt(p.reference_field.text.text.position, *at(850_000, 1_150_000))
    p.reference_field.text.text.attributes.angle.value_degrees = fpgeom.norm360(rot)
    _pt(p.value_field.text.text.position, *at(0, -1_050_000))
    p.value_field.text.text.attributes.angle.value_degrees = fpgeom.norm360(rot)
    for num, lx in (("1", -1_000_000), ("2", 1_000_000)):
        pad = bt.Pad()
        pad.number = num
        _pt(pad.position, *at(lx, 0))
        pad.pad_stack.angle.value_degrees = fpgeom.norm360(rot)
        add(pad)
    rect = bt.BoardGraphicShape()
    _pt(rect.shape.rectangle.top_left, *at(-1_700_000, -700_000))
    _pt(rect.shape.rectangle.bottom_right, *at(1_700_000, 700_000))
    add(rect)
    seg = bt.BoardGraphicShape()
    _pt(seg.shape.segment.start, *at(-333_333, 777_777))
    _pt(seg.shape.segment.end, *at(333_333, 777_777))
    add(seg)
    zone = bt.Zone()
    for lx, ly in ((-500_000, -500_000), (500_000, -500_000), (500_000, 500_000)):
        _pt(zone.outline.polygons.add().outline.nodes.add().point, *at(lx, ly))
    add(zone)
    text = bt.BoardText()
    _pt(text.text.position, *at(0, 0))
    text.text.attributes.angle.value_degrees = fpgeom.norm360(rot)
    add(text)
    if textbox:
        tb = bt.BoardTextBox()
        _pt(tb.textbox.top_left, *at(-1_000_000, -300_000))
        _pt(tb.textbox.bottom_right, *at(1_000_000, 300_000))
        add(tb)
    model = bt.Footprint3DModel()
    model.filename = "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603_1608Metric.step"
    model.visible = True
    add(model)
    return FootprintInstance(p)


def geometry(fp: FootprintInstance):
    """Every coordinate and angle of the footprint, in item order."""
    out = []

    def walk(msg):
        for f, v in msg.ListFields():
            if f.message_type is None:
                continue
            for m in (v if f.label == f.LABEL_REPEATED else [v]):
                if f.message_type.name == "Vector2":
                    out.append((f.name, m.x_nm, m.y_nm))
                elif f.message_type.name == "Angle":
                    if m.value_degrees:  # an explicit 0 is the same as unset
                        out.append((f.name, round(m.value_degrees, 9)))
                elif f.message_type.full_name == "google.protobuf.Any":
                    pass
                else:
                    walk(m)

    p = fp.proto
    walk(p)
    for item in fp.definition.items:
        out.append(type(item.proto).__name__)
        walk(item.proto)
    return out


def kinds(fp):
    return [a.type_url.rsplit(".", 1)[-1] for a in fp.proto.definition.items]


@pytest.mark.parametrize("rot", [90, 180, 270, -90, 45, 30])
def test_rotate_matches_kicad_placement(rot):
    fp = make_fp()
    assert fpgeom.place(fp, 12 * MM, 21 * MM, rot)
    want = make_fp(12 * MM, 21 * MM, fpgeom.norm180(rot))
    if rot % 90 == 0:
        assert geometry(fp) == geometry(want)
        return
    # off-axis, the rectangle becomes a polygon through its four rotated corners, as in KiCad
    rect = fp.definition.items[2].proto.shape
    assert rect.WhichOneof("geometry") == "polygon"
    corners = [(n.point.x_nm, n.point.y_nm) for n in rect.polygon.polygons[0].outline.nodes]
    x, y = 12 * MM, 21 * MM
    assert corners == [(x + a, y + b) for a, b in (fpgeom.rotate(lx, ly, rot) for lx, ly in
                       ((-1_700_000, -700_000), (1_700_000, -700_000), (1_700_000, 700_000), (-1_700_000, 700_000)))]
    skip = ("top_left", "bottom_right", "point")
    assert [g for g in geometry(fp) if g[0] not in skip] == [g for g in geometry(want) if g[0] not in skip]


def test_rotation_keeps_3d_model_and_every_item():
    fp = make_fp(textbox=True)
    before = kinds(fp)
    fpgeom.place(fp, 10 * MM, 20 * MM, 90)
    assert kinds(fp) == before
    assert "Footprint3DModel" in kinds(fp)
    assert [m.filename for m in fp.definition.models] == [
        "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603_1608Metric.step"]


def test_quarter_turns_are_exact():
    """kipy's Vector2.rotate truncates (0.85 mm came back as 0.849999 mm after a quarter turn)."""
    fp = make_fp()
    orig = geometry(fp)
    for rot in (90, 180, 270, 0):
        fpgeom.place(fp, 10 * MM, 20 * MM, rot)
    assert geometry(fp) == orig


def test_quarter_turns_off_axis_are_exact():
    fp = make_fp(rot=45)
    orig = geometry(fp)
    for rot in (135, 225, -45, 45):
        fpgeom.place(fp, 10 * MM, 20 * MM, rot)
    assert geometry(fp) == orig


def test_back_from_off_axis_is_within_a_nanometre():
    fp = make_fp()
    fpgeom.place(fp, 10 * MM, 20 * MM, 45)
    fpgeom.place(fp, 11 * MM, 20 * MM, 0)
    # the rectangle became a polygon at 45 degrees, as in KiCad; compare the rest
    skip = ("top_left", "bottom_right", "point")
    got = [g for g in geometry(fp) if g[0] not in skip]
    want = [g for g in geometry(make_fp(11 * MM, 20 * MM)) if g[0] not in skip]
    assert [g if isinstance(g, str) else g[0] for g in got] == [g if isinstance(g, str) else g[0] for g in want]
    for a, b in zip(got, want):
        if isinstance(a, tuple) and len(a) == 3:
            assert abs(a[1] - b[1]) <= 1 and abs(a[2] - b[2]) <= 1, (a, b)
        else:
            assert a == b


def test_angles_normalized_like_kicad():
    fp = make_fp(rot=180)
    fpgeom.place(fp, 10 * MM, 20 * MM, 180 + 90)  # rotate_by 90 from 180
    assert fp.orientation.degrees == -90
    assert fp.reference_field.text.attributes.angle == 270
    assert all(p.padstack.angle.degrees == 270 for p in fp.definition.pads)


def test_translation_only_is_exact_and_no_op_is_detected():
    fp = make_fp(rot=45)
    orig = geometry(fp)
    assert not fpgeom.place(fp, 10 * MM, 20 * MM, 45)
    assert not fpgeom.place(fp, 10 * MM, 20 * MM, 405)
    fpgeom.place(fp, 13 * MM, 18 * MM, 45)
    fpgeom.place(fp, 10 * MM, 20 * MM, 45)
    assert geometry(fp) == orig


def test_unsupported_child_leaves_footprint_untouched():
    fp = make_fp(textbox=True)
    orig = geometry(fp)
    with pytest.raises(fpgeom.Unsupported):
        fpgeom.place(fp, 11 * MM, 20 * MM, 45)
    assert geometry(fp) == orig


def test_mm_to_nm_rounds():
    assert nm(4.35) == 4_350_000  # kipy's from_mm gives 4349999
    assert nm(-1.005) == -1_005_000
    assert nm(0) == 0
