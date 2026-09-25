"""Live IPC tests. Need KiCad running with the API on and the board at KICAD_MCP_LIVE_PCB open in
the PCB editor. They edit that board (and save it), so point them at a scratch copy."""
import os

import pytest

PCB = os.environ.get("KICAD_MCP_LIVE_PCB", "")
pytestmark = pytest.mark.skipif(not PCB or not os.path.isfile(PCB), reason="set KICAD_MCP_LIVE_PCB to an open scratch board")


@pytest.fixture(scope="module")
def s():
    from kicad_mcp import server
    if server.kicad_status(PCB).startswith("Not connected"):
        pytest.skip("KiCad API not reachable")
    if "requested board open: yes" not in server.kicad_status(PCB):
        pytest.skip("board not open in KiCad")
    return server


def _row(out, ref):
    lines = out.split("\n")
    header = [c.strip() for c in next(l for l in lines if l.startswith("| ref")).strip("|").split("|")]
    cells = [c.strip() for c in next(l for l in lines if l.startswith(f"| {ref} |")).strip("|").split("|")]
    d = dict(zip(header, cells))
    return {"x": float(d["x"]), "y": float(d["y"]), "rot": float(d["rot"]), "side": d["side"]}


def _first_ref(s):
    return s.list_components(PCB, limit=1).split("\n")[2].split("|")[1].strip()


def test_move_and_restore(s):
    ref = _first_ref(s)
    before = _row(s.list_components(PCB, ref=ref), ref)
    moved = _row(s.move_component(PCB, ref=ref, dx=1.0, dy=-0.5), ref)
    assert abs(moved["x"] - before["x"] - 1.0) < 1e-3 and abs(moved["y"] - before["y"] + 0.5) < 1e-3
    back = _row(s.move_component(PCB, ref=ref, x=before["x"], y=before["y"]), ref)
    assert abs(back["x"] - before["x"]) < 1e-3


def test_flip_with_move_in_one_batch(s):
    ref = _first_ref(s)
    before = _row(s.list_components(PCB, ref=ref), ref)
    out = _row(s.move_components(PCB, moves=[{"ref": ref, "flip": True, "dx": 0.25}]), ref)
    assert out["side"] != before["side"] and abs(out["x"] - before["x"] - 0.25) < 1e-3
    back = _row(s.move_components(PCB, moves=[{"ref": ref, "flip": True, "dx": -0.25}]), ref)
    assert back["side"] == before["side"] and abs(back["x"] - before["x"]) < 1e-3


def test_track_via_ripup_roundtrip(s):
    net = s.list_nets(PCB, limit=1, sort="name").split("\n")[2].split("|")[1].strip()
    assert "segments: 2" in s.add_track(PCB, net=net, points=[[1, 1], [2, 1], [2, 2]], layer="F.Cu")
    assert "diameter_mm" in s.add_via(PCB, net=net, x=2, y=2)
    out = s.ripup_net(PCB, net=net)
    assert "tracks_removed" in out and "vias_removed" in out


def test_selection_and_save(s):
    ref = _first_ref(s)
    assert s.select_components(PCB, refs=[ref]).startswith("Selected:")
    assert ref in s.get_selection(PCB)
    assert s.save_board(PCB).startswith("Saved")
    assert "Error" not in s.live_component(PCB, ref=ref)
