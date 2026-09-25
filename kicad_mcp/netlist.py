"""Parse a kicadxml netlist into plain dicts. Cached per source path + mtime."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_REF_RE = re.compile(r"^([A-Za-z_#]+)(\d*)(.*)$")


def ref_key(ref: str):
    m = _REF_RE.match(ref or "")
    if not m:
        return (ref, 0, "")
    return (m.group(1).upper(), int(m.group(2) or 0), m.group(3))


@dataclass
class Netlist:
    source: str = ""
    sheets: list[dict] = field(default_factory=list)
    components: dict[str, dict] = field(default_factory=dict)  # ref -> comp
    nets: dict[str, dict] = field(default_factory=dict)  # name -> {code, nodes:[...]}
    libparts: dict[tuple[str, str], dict] = field(default_factory=dict)

    def pins_of(self, ref: str) -> list[dict]:
        """All pins of a component from its libpart, joined with the net each is on."""
        c = self.components[ref]
        lp = self.libparts.get((c["lib"], c["part"]), {})
        on_net = {}
        for name, n in self.nets.items():
            for node in n["nodes"]:
                if node["ref"] == ref:
                    on_net[node["pin"]] = name
        pins = []
        for p in lp.get("pins", []):
            pins.append({**p, "net": on_net.get(p["num"], "")})
        seen = {p["num"] for p in pins}
        for num, net in on_net.items():
            if num not in seen:
                pins.append({"num": num, "name": "", "type": "", "net": net})
        pins.sort(key=lambda p: ref_key(p["num"]) if p["num"][:1].isalpha() else ("", int(re.sub(r"\D", "", p["num"]) or 0), p["num"]))
        return pins


_cache: dict[str, tuple[float, Netlist]] = {}


def parse(xml_path: Path) -> Netlist:
    mt = xml_path.stat().st_mtime
    ent = _cache.get(str(xml_path))
    if ent and ent[0] == mt:
        return ent[1]
    root = ET.parse(xml_path).getroot()
    nl = Netlist()
    design = root.find("design")
    if design is not None:
        nl.source = design.findtext("source", "")
        for s in design.findall("sheet"):
            tb = s.find("title_block")
            nl.sheets.append({
                "number": int(s.get("number", "0")),
                "name": s.get("name", ""),
                "file": tb.findtext("source", "") if tb is not None else "",
                "title": tb.findtext("title", "") if tb is not None else "",
                "rev": tb.findtext("rev", "") if tb is not None else "",
            })
    for c in root.iter("comp"):
        ref = c.get("ref", "")
        props = {p.get("name", ""): (p.get("value") if p.get("value") is not None else True) for p in c.findall("property")}
        ls = c.find("libsource")
        sp = c.find("sheetpath")
        nl.components[ref] = {
            "ref": ref,
            "value": c.findtext("value", ""),
            "footprint": c.findtext("footprint", ""),
            "datasheet": c.findtext("datasheet", ""),
            "lib": ls.get("lib", "") if ls is not None else "",
            "part": ls.get("part", "") if ls is not None else "",
            "description": ls.get("description", "") if ls is not None else "",
            "fields": {f.get("name", ""): (f.text or "") for f in c.findall("fields/field")},
            "sheet": sp.get("names", "/") if sp is not None else "/",
            "dnp": "dnp" in props,
            "exclude_from_bom": "exclude_from_bom" in props,
            "exclude_from_board": "exclude_from_board" in props,
        }
    for lp in root.iter("libpart"):
        key = (lp.get("lib", ""), lp.get("part", ""))
        nl.libparts[key] = {
            "description": lp.findtext("description", ""),
            "pins": [{"num": p.get("num", ""), "name": p.get("name", ""), "type": p.get("type", "")} for p in lp.findall("pins/pin")],
        }
    for n in root.iter("net"):
        name = n.get("name", "")
        nl.nets[name] = {
            "code": int(n.get("code", "0")),
            "name": name,
            "nodes": [{"ref": nd.get("ref", ""), "pin": nd.get("pin", ""), "function": nd.get("pinfunction", ""),
                       "type": nd.get("pintype", "")} for nd in n.findall("node")],
        }
    _cache[str(xml_path)] = (mt, nl)
    return nl
