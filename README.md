# kicad-mcp

An MCP server that gives coding agents a compact, read-only view of KiCad projects:
measurements, component and net queries, DRC/ERC summaries, BOM, and renders, without
dumping 20k-line `.kicad_pcb` / `.kicad_sch` files into context.

It does not reimplement KiCad. It sits on the three machine interfaces KiCad already ships:

| Surface | Used for |
|---|---|
| `pcbnew` Python module (KiCad's bundled Python) | board geometry: footprints, pads, nets, tracks, zones, distances |
| `kicad-cli` | DRC, ERC, netlist export, SVG/3D renders |
| netlist XML (from `kicad-cli sch export netlist`) | schematic symbols, pins, nets, BOM |

## Architecture

```
agent ──MCP stdio──> kicad_mcp/server.py   (Python 3.10+, mcp SDK)
                        ├─ worker_client ──JSON lines──> worker/board_worker.py  (KiCad's Python 3.9, imports pcbnew)
                        ├─ cli.py           ──subprocess──> kicad-cli  (DRC/ERC/netlist/render, cached by mtime)
                        └─ netlist.py       parses the netlist XML
```

The `pcbnew` module is compiled against KiCad's own Python (3.9 on macOS), and the MCP SDK
needs 3.10+, so board queries run in a persistent worker subprocess under KiCad's interpreter.
Boards are cached in the worker and reloaded when the file changes.

## Tools

All tools take `path`: a project directory, `.kicad_pro`, `.kicad_pcb` or `.kicad_sch`
(default: current directory). Units are mm, KiCad coordinates (y down).

| Tool | What it returns |
|---|---|
| `project_info` | files, board size/counts, schematic sheets. Start here. |
| `board_summary` | outline, layers, counts, zones, design rules, netclasses |
| `list_components` | footprints with position/rotation/side; filters by ref/value/footprint/side/dnp |
| `get_component` | one footprint: bbox, courtyard, fields, 3D model, every pad with its net |
| `measure` | distance between refs, pads (`U7.3`) or points (`120,44`): centre, dx/dy, edge gap |
| `neighbors` | footprints within a radius, with courtyard gap and direction |
| `net_info` | pads, track length per layer, widths, vias, zones, netclass for one net |
| `list_nets` | nets with pad count, routed length, via count |
| `items_in_region` | what lies in a rectangle, optionally on one layer |
| `search` | where a string appears: refs, values, fields, nets, silkscreen |
| `drc` / `erc` | counts per violation type; `type=` lists individual findings |
| `render_3d` | PNG of the board (top/bottom/…) |
| `render_layers` | SVG (+PNG on macOS) of chosen layers |
| `schematic_summary` | sheets, symbol counts, nets, DNP |
| `sch_components` / `sch_component` | symbols; one symbol with every pin and its net |
| `sch_nets` / `sch_net` | schematic nets and their pins |
| `bom` | grouped bill of materials with part-number fields |

## Install

Requires KiCad 9+ (tested with 10.0.6 on macOS) and Python 3.10+.

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Register with Claude Code (user scope, so it is available in every hardware project):

```bash
claude mcp add --scope user kicad -- /ABS/PATH/kicad-mcp/.venv/bin/python -m kicad_mcp
```

or add to `~/.claude.json`:

```json
{"mcpServers": {"kicad": {"type": "stdio", "command": "/ABS/PATH/kicad-mcp/.venv/bin/python", "args": ["-m", "kicad_mcp"]}}}
```

Environment overrides: `KICAD_APP` (macOS .app bundle), `KICAD_CLI`, `KICAD_PYTHON`
(interpreter that can `import pcbnew`), `KICAD_MCP_CACHE` (default `~/.cache/kicad-mcp`),
`KICAD_MCP_DEBUG=1` to print worker tracebacks to stderr.

## Tests

```bash
KICAD_MCP_TEST_PROJECT=/path/to/a/kicad/project .venv/bin/pytest
```

## Roadmap

- Editing (move/rotate footprints, route hints) through the KiCad IPC API (`kicad-python`),
  which is the supported write path with undo support in a running KiCad session.
- Schematic editing, pending IPC API coverage for schematics.
- Region-cropped renders.
