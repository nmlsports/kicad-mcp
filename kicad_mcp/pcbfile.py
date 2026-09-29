"""Footprint-level comparison of a .kicad_pcb before and after a save.

KiCad's IPC API (10.0.x) cannot carry everything a footprint holds. UpdateItems replaces the
footprint with one rebuilt from the API message, and that message has no field for
- the symbol unit -> pin map, (units ...), used for gate swapping;
- pad fabrication properties, (property pad_prop_bga) etc. (the Pad message gains fab_property
  after 10.0.6);
- embedded files, component classes, and groups and points inside the footprint.
So any API edit of a footprint (move, rotate, attributes, fields) drops them. save() uses this
module to copy lost units and pad properties back from the file it overwrote, matched by UUID, and
to report whatever else went missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# footprint children the API round trip drops (or dropped before: 3D models, via kipy)
LOSSY = ("model", "units", "embedded_files", "component_classes", "group", "point")

_TOKENS = re.compile(r'"(?:[^"\\]|\\.)*"|[()]')
_HEAD = re.compile(r'\(\s*([^\s()"]+)')
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


@dataclass
class Node:
    token: str
    start: int
    end: int


def children(text: str, start: int = 0, end: int = -1) -> list[Node]:
    """Direct children of the s-expression text[start:end] (the whole file: of (kicad_pcb))."""
    out: list[Node] = []
    depth, cs = 0, 0
    for m in _TOKENS.finditer(text, start, len(text) if end < 0 else end):
        tok = m.group()
        if tok == "(":
            depth += 1
            if depth == 2:
                cs = m.start()
        elif tok == ")":
            if depth == 2:
                out.append(Node(_HEAD.match(text, cs).group(1), cs, m.end()))
            depth -= 1
    return out


@dataclass
class Footprint:
    node: Node
    lib_id: str
    uuid: str
    ref: str
    children: list[Node]

    def all(self, token: str) -> list[Node]:
        return [c for c in self.children if c.token == token]


def _uuid(text: str, kids: list[Node]) -> str:
    n = next((c for c in kids if c.token == "uuid"), None)
    return _STRING.search(text, n.start, n.end).group(1) if n else ""


def footprints(text: str) -> list[Footprint]:
    out = []
    for n in children(text):
        if n.token != "footprint":
            continue
        kids = children(text, n.start, n.end)
        lib = _STRING.search(text, n.start, kids[0].start if kids else n.end)
        ref = ""
        for c in kids:
            strings = _STRING.findall(text, c.start, c.end) if c.token == "property" else []
            if strings[:1] == ["Reference"] and len(strings) > 1:
                ref = strings[1]
                break
        out.append(Footprint(n, lib.group(1) if lib else "", _uuid(text, kids), ref, kids))
    return out


def _pads(text: str, fp: Footprint) -> dict[str, list[Node]]:
    """{pad uuid: pad children}"""
    out = {}
    for p in fp.all("pad"):
        kids = children(text, p.start, p.end)
        out[_uuid(text, kids)] = kids
    return out


def _matched(before: str, after: str):
    old = {f.uuid: f for f in footprints(before)}
    for f in footprints(after):
        o = old.get(f.uuid)
        if o is not None:
            yield o, f


def lost_data(before: str, after: str) -> dict[str, list[str]]:
    """{ref: ["model", "units", "3 pad properties", ...]} for footprints (matched by UUID) that
    have fewer LOSSY children, or fewer pads with a fabrication property, in `after`."""
    report: dict[str, list[str]] = {}
    for o, f in _matched(before, after):
        lost = []
        for tok in LOSSY:
            n = len(o.all(tok)) - len(f.all(tok))
            if n > 0:
                lost.append(f"{n} {tok}" if n > 1 else tok)
        n = _prop_count(before, o) - _prop_count(after, f)
        if n > 0:
            lost.append(f"{n} pad propert{'ies' if n > 1 else 'y'}")
        if lost:
            report[f.ref or f.uuid] = lost
    return report


def _prop_count(text: str, fp: Footprint) -> int:
    return sum(any(c.token == "property" for c in kids) for kids in _pads(text, fp).values())


def _reinsert(before: str, old: list[Node], new: list[Node], token: str):
    """(position in `after`, text) that puts old's `token` child back into new, after the
    sibling it followed in `before`; None if new already has one or there is no such sibling."""
    i = next((k for k, c in enumerate(old) if c.token == token), None)
    if i is None or i == 0 or any(c.token == token for c in new):
        return None
    anchors = [c for c in new if c.token == old[i - 1].token]
    if not anchors:
        return None
    c = old[i]
    indent = before[before.rfind("\n", 0, c.start) + 1:c.start]
    return anchors[-1].end, "\n" + indent + before[c.start:c.end]


def restore(before: str, after: str) -> tuple[str, dict[str, list[str]]]:
    """Copy (units ...) and pad (property pad_prop_...) that the API dropped from `before` into
    the same footprints (same UUID and library footprint) and pads (same UUID) of `after`.
    Returns the new text and {ref: [what was restored]}."""
    edits: list[tuple[int, str]] = []
    restored: dict[str, list[str]] = {}
    for o, f in _matched(before, after):
        if o.lib_id != f.lib_id:
            continue
        what = []
        e = _reinsert(before, o.children, f.children, "units")
        if e:
            edits.append(e)
            what.append("units")
        new_pads = _pads(after, f)
        props = 0
        for pad_uuid, old_kids in _pads(before, o).items():
            e = _reinsert(before, old_kids, new_pads.get(pad_uuid, []), "property") if pad_uuid else None
            if e:
                edits.append(e)
                props += 1
        if props:
            what.append(f"{props} pad propert{'ies' if props > 1 else 'y'}")
        if what:
            restored[f.ref or f.uuid] = what
    for pos, block in sorted(edits, reverse=True):
        after = after[:pos] + block + after[pos:]
    return after, restored
