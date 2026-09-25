"""Smoke tests against a real project. Set KICAD_MCP_TEST_PROJECT to a KiCad project dir; skipped otherwise."""
import os

import pytest

PROJECT = os.environ.get("KICAD_MCP_TEST_PROJECT", "")
pytestmark = pytest.mark.skipif(not PROJECT or not os.path.isdir(PROJECT), reason="set KICAD_MCP_TEST_PROJECT to a KiCad project dir")


@pytest.fixture(scope="module")
def s():
    from kicad_mcp import server
    return server


def test_project_info(s):
    out = s.project_info(PROJECT)
    assert "Board:" in out and "Schematic:" in out and "Error" not in out


def test_board_summary(s):
    out = s.board_summary(PROJECT)
    assert "outline:" in out and "Netclasses:" in out


def test_list_and_get(s):
    out = s.list_components(PROJECT, ref="R*", limit=3)
    assert out.count("\n| R") == 3
    ref = out.split("\n")[2].split("|")[1].strip()
    detail = s.get_component(PROJECT, ref=ref)
    assert "Pads (" in detail and "courtyard:" in detail


def test_measure_kinds(s):
    a, b = [l.split("|")[1].strip() for l in s.list_components(PROJECT, limit=2).split("\n")[2:4]]
    assert "centre distance" in s.measure(PROJECT, a=a, b=b)
    assert "edge gap" in s.measure(PROJECT, a=f"{a}.1", b=f"{b}.1")
    assert "edge gap" in s.measure(PROJECT, a="0,0", b=a)


def test_net_roundtrip(s):
    first = s.list_nets(PROJECT, limit=1).split("\n")[2].split("|")[1].strip()
    out = s.net_info(PROJECT, net=first)
    assert f"net: {first}" in out and "Pads (" in out


def test_errors_are_short(s):
    assert s.get_component(PROJECT, ref="NOPE123").startswith("Error: No footprint")
    assert s.board_summary("/definitely/not/here").startswith("Error:")


def test_schematic(s):
    assert "Sheets:" in s.schematic_summary(PROJECT)
    ref = s.sch_components(PROJECT, limit=1).split("\n")[2].split("|")[1].strip()
    assert "Pins (" in s.sch_component(PROJECT, ref=ref)
    assert "line items" in s.bom(PROJECT)


def test_checks(s):
    assert "DRC findings" in s.drc(PROJECT)
    assert "ERC findings" in s.erc(PROJECT)
