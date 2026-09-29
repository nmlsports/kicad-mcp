"""Post-save footprint checks in kicad_mcp.pcbfile on a small .kicad_pcb; no KiCad needed."""
from kicad_mcp import pcbfile

FP = '''	(footprint "Resistor_SMD:R_0201_0603Metric"
		(layer "F.Cu")
		(uuid "{uuid}")
		(at {at})
		(property "Reference" "{ref}"
			(at 0 -1.05 0)
			(layer "F.SilkS")
			(uuid "11111111-0000-0000-0000-{n:012d}")
		)
		(property "Value" "(1k) \\"x\\""
			(at 0 1.05 0)
			(layer "F.Fab")
			(uuid "22222222-0000-0000-0000-{n:012d}")
		)
		(path "/970035c7-e1ad-4880-8b81-eac7a5c83363/8b069395-9c7c-4da4-aefa-5e3d9e27521{n}")
		(sheetname "/Power/")
		(sheetfile "power.kicad_sch")
{units}		(attr smd)
		(pad "1" smd roundrect
			(at -0.32 0)
			(size 0.46 0.4)
{prop}			(layers "F.Cu" "F.Mask" "F.Paste")
			(uuid "33333333-0000-0000-0000-{n:012d}")
		)
{model}	)
'''
UNITS = '''		(units
			(unit
				(name "A")
				(pins "1" "2")
			)
		)
'''
MODEL = '''		(model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0201_0603Metric.step"
			(offset
				(xyz 0 0 0)
			)
		)
'''


def board(*fps):
    return '(kicad_pcb\n\t(version 20260206)\n\t(generator "pcbnew")\n' + "".join(fps) + ")\n"


PROP = "\t\t\t(property pad_prop_bga)\n"


def fp(n, at="10 20 90", units=True, model=True, prop=True):
    return FP.format(uuid=f"aaaaaaaa-0000-0000-0000-{n:012d}", ref=f"R{n}", n=n, at=at,
                     units=UNITS if units else "", model=MODEL if model else "", prop=PROP if prop else "")


def test_parse():
    fps = pcbfile.footprints(board(fp(1), fp(2, units=False)))
    assert [(f.ref, f.lib_id) for f in fps] == [("R1", "Resistor_SMD:R_0201_0603Metric"),
                                                ("R2", "Resistor_SMD:R_0201_0603Metric")]
    assert fps[0].uuid.endswith("000000000001")
    assert [c.token for c in fps[0].children if c.token != "property"] == [
        "layer", "uuid", "at", "path", "sheetname", "sheetfile", "units", "attr", "pad", "model"]
    assert not fps[1].all("units")


def test_lost_data_reports_models_units_and_pad_properties_by_uuid():
    before = board(fp(1), fp(2), fp(3))
    after = board(fp(1, at="11 20 180", units=False, model=False), fp(2, units=False, prop=False), fp(3, at="0 0"))
    assert pcbfile.lost_data(before, after) == {"R1": ["model", "units"], "R2": ["units", "1 pad property"]}
    assert pcbfile.lost_data(before, before) == {}


def test_restore_puts_back_the_exact_blocks():
    before = board(fp(1), fp(2), fp(3, units=False, prop=False))
    after = board(fp(1, at="11 20 180", units=False, prop=False), fp(2, prop=False), fp(3, units=False, prop=False))
    fixed, restored = pcbfile.restore(before, after)
    assert restored == {"R1": ["units", "1 pad property"], "R2": ["1 pad property"]}
    assert fixed == board(fp(1, at="11 20 180"), fp(2), fp(3, units=False, prop=False))
    assert pcbfile.lost_data(before, fixed) == {}
    assert pcbfile.restore(before, fixed) == (fixed, {})


def test_restore_skips_replaced_footprints():
    before = board(fp(1))
    after = board(fp(1, units=False, prop=False).replace("R_0201_0603Metric\"", "R_0402_1005Metric\"", 1))
    assert pcbfile.restore(before, after) == (after, {})
