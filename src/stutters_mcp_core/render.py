"""Topology diagrams as Mermaid or draw.io text, shared by every server (roadmap C65).

A server turns what it read into a `Graph` (nodes, edges, optional groups such as sites or VPN hubs)
and asks for a format; the renderer does the escaping, shapes, status colours and layout, so every
server draws the same way:

    from stutters_mcp_core.render import Edge, Graph, Node, diagram_result

    g = Graph(title="SD-WAN fabric", direction="LR")
    g.nodes += [Node("hub1", lines=["system 192.0.2.1"], group="site 100", shape="hexagon"),
                Node("br1", lines=["system 192.0.2.11"], group="site 200", status="warn")]
    g.edges.append(Edge("br1", "hub1", label="mpls / mpls", status="down"))
    return diagram_result(g, "mermaid")   # or "drawio"

`Graph.from_dict()` accepts the same structure as plain dicts (`{"nodes": [{"id": ...}], "edges": [{"a":
..., "b": ...}]}`), which is what a server's own summary usually is already. Nodes with `x`/`y` keep
their positions in draw.io (CML canvas coordinates); others are laid out in a grid, one column per group.

Mermaid output is a `graph` flowchart; paste it into a Markdown ```mermaid block. draw.io output is an
uncompressed `.drawio` file (diagrams.net). Neither embeds anything but the labels it was given: callers
pass names and addresses, never secrets.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, get_args
from xml.sax.saxutils import quoteattr

from fastmcp.tools import ToolResult

from stutters_mcp_core.errors import InvalidRequest
from stutters_mcp_core.formatting import text_result

DiagramFormat = Literal["mermaid", "drawio"]
Shape = Literal["box", "round", "circle", "hexagon", "cylinder", "cloud"]
Status = Literal["ok", "warn", "down", "unknown"]
Direction = Literal["LR", "TB", "RL", "BT"]

FORMATS: tuple[str, ...] = get_args(DiagramFormat)
SHAPES: tuple[str, ...] = get_args(Shape)
STATUSES: tuple[str, ...] = get_args(Status)

# Fill / stroke per status (draw.io and Mermaid classDef); colour-blind safe enough with the dashed edge.
_COLOURS: dict[str, tuple[str, str]] = {
    "ok": ("#d5e8d4", "#82b366"),
    "warn": ("#fff2cc", "#d6b656"),
    "down": ("#f8cecc", "#b85450"),
    "unknown": ("#f5f5f5", "#999999"),
}
_DEFAULT_FILL = ("#f5f5f5", "#666666")
MAX_NODES = 2000  # beyond this neither format is readable; the caller should narrow (site=, device=)


@dataclass
class Node:
    id: str
    label: str = ""  # defaults to the id
    lines: list[str] = field(default_factory=list)  # extra label lines: role, address, version
    shape: Shape = "box"
    group: str | None = None  # site, area, hub/spoke: a Mermaid subgraph / a draw.io column
    status: Status | None = None
    x: float | None = None
    y: float | None = None

    @property
    def text(self) -> str:
        return self.label or self.id


@dataclass
class Edge:
    a: str
    b: str
    label: str = ""
    status: Status | None = None  # "down" draws red and dashed
    directed: bool = False


@dataclass
class Graph:
    title: str = "topology"
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # a legend / caveats box
    direction: Direction = "LR"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Graph:
        """Build from plain dicts: {"title", "direction", "notes", "nodes": [{"id", "label", "lines",
        "shape", "group", "status", "x", "y"}], "edges": [{"a", "b", "label", "status", "directed"}]}.
        Unknown keys are ignored; unknown shapes/statuses fall back to box/None."""
        g = cls(
            title=str(data.get("title") or "topology"),
            notes=[str(n) for n in data.get("notes") or []],
            direction=_pick(data.get("direction"), get_args(Direction), "LR"),  # type: ignore[arg-type]
        )
        for n in data.get("nodes") or []:
            g.nodes.append(
                Node(
                    id=str(n["id"]),
                    label=str(n.get("label") or ""),
                    lines=[str(x) for x in n.get("lines") or [] if x not in (None, "")],
                    shape=_pick(n.get("shape"), SHAPES, "box"),  # type: ignore[arg-type]
                    group=str(n["group"]) if n.get("group") not in (None, "") else None,
                    status=_pick(n.get("status"), STATUSES, None),  # type: ignore[arg-type]
                    x=_num(n.get("x")),
                    y=_num(n.get("y")),
                )
            )
        for e in data.get("edges") or []:
            g.edges.append(
                Edge(
                    a=str(e["a"]),
                    b=str(e["b"]),
                    label=str(e.get("label") or ""),
                    status=_pick(e.get("status"), STATUSES, None),  # type: ignore[arg-type]
                    directed=bool(e.get("directed")),
                )
            )
        return g

    def check(self) -> None:
        """Refuse what neither format can draw sensibly: duplicate ids, too many nodes."""
        if len(self.nodes) > MAX_NODES:
            raise InvalidRequest(
                f"{len(self.nodes)} nodes is too many to draw (limit {MAX_NODES}): narrow the request"
            )
        seen: set[str] = set()
        for n in self.nodes:
            if n.id in seen:
                raise InvalidRequest(f"Duplicate node id {n.id!r} in the diagram")
            seen.add(n.id)

    def groups(self) -> list[str | None]:
        """Group names in first-seen order (None = ungrouped nodes)."""
        out: list[str | None] = []
        for n in self.nodes:
            if n.group not in out:
                out.append(n.group)
        return out


def _pick(value: Any, allowed: Iterable[str], default: str | None) -> str | None:
    return value if isinstance(value, str) and value in tuple(allowed) else default


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# --- shared text helpers ------------------------------------------------------------------

_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_SHORT_IF = (
    ("HundredGigE", "Hu"),
    ("FortyGigabitEthernet", "Fo"),
    ("TwentyFiveGigE", "Twe"),
    ("TenGigabitEthernet", "Te"),
    ("GigabitEthernet", "Gi"),
    ("FastEthernet", "Fa"),
    ("Ethernet", "E"),
    ("Loopback", "Lo"),
    ("Management", "Mgmt"),
    ("Port-channel", "Po"),
    ("Tunnel", "Tu"),
)


def short_if(name: str) -> str:
    """'GigabitEthernet1/0/1' -> 'Gi1/0/1', 'Loopback0' -> 'Lo0'; other names unchanged."""
    for long, short in _SHORT_IF:
        if name.startswith(long):
            return short + name[len(long) :]
    return name


def mermaid_text(s: str) -> str:
    """Safe inside a quoted Mermaid label: one line, entity-escaped (# first, then the rest)."""
    s = re.sub(r"[\r\n\t]+", " ", _XML_BAD.sub("", s))
    return (
        s.replace("#", "#35;")
        .replace("&", "#amp;")
        .replace('"', "#quot;")
        .replace("<", "#lt;")
        .replace(">", "#gt;")
        .replace("|", "#124;")
    )


def html_text(s: str) -> str:
    """HTML-escaped text for draw.io html=1 labels (control characters XML forbids removed)."""
    return _XML_BAD.sub("", s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Mermaid ----------------------------------------------------------------------------------

_MERMAID_SHAPE = {
    "box": ('["', '"]'),
    "round": ('("', '")'),
    "circle": ('(("', '"))'),
    "hexagon": ('{{"', '"}}'),
    "cylinder": ('[("', '")]'),
    "cloud": ('>"', '"]'),  # Mermaid has no cloud in graph syntax; the asymmetric flag reads as "outside"
}


def to_mermaid(graph: Graph) -> str:
    """A Mermaid flowchart: groups as subgraphs, status as classes (ok/warn/down/unknown), edges with
    their labels; a down edge is dashed and red."""
    graph.check()
    ids = {n.id: f"n{i}" for i, n in enumerate(graph.nodes)}
    lines = [f"graph {graph.direction}"]

    def node_line(n: Node, indent: str) -> str:
        label = "<br/>".join(mermaid_text(p) for p in [n.text, *n.lines])
        left, right = _MERMAID_SHAPE[n.shape]
        cls = f":::{n.status}" if n.status else ""
        return f"{indent}{ids[n.id]}{left}{label}{right}{cls}"

    for gi, group in enumerate(graph.groups()):
        members = [n for n in graph.nodes if n.group == group]
        if group is None:
            lines += [node_line(n, "  ") for n in members]
            continue
        lines.append(f'  subgraph g{gi}["{mermaid_text(group)}"]')
        lines += [node_line(n, "    ") for n in members]
        lines.append("  end")
    down: list[int] = []
    drawn = 0
    for e in graph.edges:
        a, b = ids.get(e.a), ids.get(e.b)
        if not (a and b):
            continue  # an edge to a node outside the (narrowed) graph
        arrow = ("-.->" if e.directed else "-.-") if e.status == "down" else ("-->" if e.directed else "---")
        text = f'|"{mermaid_text(e.label)}"|' if e.label else ""
        lines.append(f"  {a} {arrow}{text} {b}")
        if e.status == "down":
            down.append(drawn)
        drawn += 1
    if graph.notes:
        note = "<br/>".join(mermaid_text(t) for t in graph.notes)
        lines.append(f'  notes["{note}"]')
        lines.append("  style notes fill:#fffbe6,stroke:#999,stroke-dasharray: 3 3")
    for status in sorted({n.status for n in graph.nodes if n.status}):
        fill, stroke = _COLOURS[status]
        lines.append(f"  classDef {status} fill:{fill},stroke:{stroke}")
    if down:
        lines.append(f"  linkStyle {','.join(map(str, down))} stroke:#b85450,stroke-width:2px")
    return "\n".join(lines) + "\n"


# --- draw.io ----------------------------------------------------------------------------------

_DRAWIO_SHAPE = {
    "box": "rounded=1;whiteSpace=wrap;html=1;",
    "round": "rounded=1;arcSize=50;whiteSpace=wrap;html=1;",
    "circle": "ellipse;whiteSpace=wrap;html=1;",
    "hexagon": "shape=hexagon;perimeter=hexagonPerimeter2;whiteSpace=wrap;html=1;",
    "cylinder": "shape=cylinder3;whiteSpace=wrap;html=1;",
    "cloud": "ellipse;shape=cloud;whiteSpace=wrap;html=1;",
}
NODE_W = 160
COL_GAP = 120
ROW_GAP = 40
MARGIN = 80


def _height(n: Node) -> int:
    return 40 + 14 * len(n.lines)


def layout(graph: Graph) -> dict[str, tuple[float, float]]:
    """Top-left (x, y) per node id. Nodes that all carry x/y keep them (shifted to the margin);
    otherwise one column per group, nodes stacked in order."""
    if graph.nodes and all(n.x is not None and n.y is not None for n in graph.nodes):
        x0 = min(float(n.x or 0) for n in graph.nodes)
        y0 = min(float(n.y or 0) for n in graph.nodes)
        return {
            n.id: (float(n.x or 0) - x0 + MARGIN, float(n.y or 0) - y0 + MARGIN - _height(n) / 2)
            for n in graph.nodes
        }
    pos: dict[str, tuple[float, float]] = {}
    for col, group in enumerate(graph.groups()):
        y = float(MARGIN + (30 if group is not None else 0))
        for n in (n for n in graph.nodes if n.group == group):
            pos[n.id] = (MARGIN + col * (NODE_W + COL_GAP), y)
            y += _height(n) + ROW_GAP
    return pos


def to_drawio(graph: Graph, *, host: str = "stutters-mcp-core") -> str:
    """A draw.io (diagrams.net) file: groups as labelled containers behind their nodes, status as fill
    colours, edges labelled; a down edge is dashed and red."""
    graph.check()
    pos = layout(graph)
    ids = {n.id: f"n{i}" for i, n in enumerate(graph.nodes)}
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    for gi, group in enumerate(graph.groups()):
        if group is None:
            continue
        members = [n for n in graph.nodes if n.group == group]
        xs = [pos[n.id][0] for n in members]
        ys = [pos[n.id][1] for n in members]
        bottom = max(pos[n.id][1] + _height(n) for n in members)
        x, y = min(xs) - 20, min(ys) - 40
        w, h = max(xs) - min(xs) + NODE_W + 40, bottom - y + 20
        cells.append(
            f'<mxCell id="g{gi}" value={quoteattr(html_text(group))} style="rounded=1;html=1;dashed=1;'
            'fillColor=none;strokeColor=#999999;verticalAlign=top;align=left;spacingLeft=8;fontStyle=1;" '
            f'vertex="1" parent="1"><mxGeometry x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" '
            'as="geometry"/></mxCell>'
        )
    for n in graph.nodes:
        fill, stroke = _COLOURS[n.status] if n.status else _DEFAULT_FILL
        value = "<br>".join([f"<b>{html_text(n.text)}</b>", *(html_text(x) for x in n.lines)])
        x, y = pos[n.id]
        cells.append(
            f'<mxCell id="{ids[n.id]}" value={quoteattr(value)} '
            f'style="{_DRAWIO_SHAPE[n.shape]}fillColor={fill};strokeColor={stroke};" vertex="1" parent="1">'
            f'<mxGeometry x="{x:.0f}" y="{y:.0f}" width="{NODE_W}" height="{_height(n)}" as="geometry"/>'
            "</mxCell>"
        )
    for i, e in enumerate(graph.edges):
        a, b = ids.get(e.a), ids.get(e.b)
        if not (a and b):
            continue
        style = "html=1;fontSize=9;" + ("endArrow=classic;" if e.directed else "endArrow=none;")
        if e.status == "down":
            style += "dashed=1;strokeColor=#b85450;strokeWidth=2;"
        elif e.status in _COLOURS:
            style += f"strokeColor={_COLOURS[e.status][1]};"
        cells.append(
            f'<mxCell id="e{i}" value={quoteattr(html_text(e.label))} style="{style}" edge="1" parent="1" '
            f'source="{a}" target="{b}"><mxGeometry relative="1" as="geometry"/></mxCell>'
        )
    if graph.notes:
        text = "<br>".join(html_text(t) for t in graph.notes)
        right = max((x for x, _ in pos.values()), default=0) + NODE_W + COL_GAP
        cells.append(
            f'<mxCell id="notes" value={quoteattr(text)} style="text;html=1;align=left;verticalAlign=top;'
            'fontSize=10;fillColor=#fffbe6;strokeColor=#999999;dashed=1;" vertex="1" parent="1">'
            f'<mxGeometry x="{right:.0f}" y="{MARGIN}" width="260" height="{20 + 16 * len(graph.notes)}" '
            'as="geometry"/></mxCell>'
        )
    title = quoteattr(_XML_BAD.sub("", graph.title))
    return (
        f"<mxfile host={quoteattr(_XML_BAD.sub('', host))}><diagram name={title}>"
        "<mxGraphModel><root>" + "".join(cells) + "</root></mxGraphModel></diagram></mxfile>\n"
    )


# --- entry points -----------------------------------------------------------------------------


def render(graph: Graph | Mapping[str, Any], fmt: str, *, host: str = "stutters-mcp-core") -> str:
    """The diagram as text in `fmt` ('mermaid' or 'drawio')."""
    g = graph if isinstance(graph, Graph) else Graph.from_dict(graph)
    if fmt == "mermaid":
        return to_mermaid(g)
    if fmt == "drawio":
        return to_drawio(g, host=host)
    raise InvalidRequest(f"Unknown diagram format {fmt!r}: use one of {', '.join(FORMATS)}")


def diagram_result(
    graph: Graph | Mapping[str, Any], fmt: str, *, host: str = "stutters-mcp-core"
) -> ToolResult:
    """`render` wrapped as a tool result. Mermaid is fenced so chat clients draw it; draw.io is returned
    bare so it can be saved as a .drawio file unchanged."""
    text = render(graph, fmt, host=host)
    if fmt == "mermaid":
        text = f"```mermaid\n{text}```\n"
    return text_result(text)
