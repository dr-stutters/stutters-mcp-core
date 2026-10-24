"""C65: the shared Mermaid / draw.io topology renderer."""

import xml.etree.ElementTree as ET

import pytest

from stutters_mcp_core import InvalidRequest
from stutters_mcp_core.render import (
    MAX_NODES,
    Edge,
    Graph,
    Node,
    diagram_result,
    layout,
    mermaid_text,
    render,
    short_if,
    to_drawio,
    to_mermaid,
)


def fabric() -> Graph:
    g = Graph(title="SD-WAN fabric", notes=["dashed = BFD down"])
    g.nodes += [
        Node("hub1", lines=["system 192.0.2.1"], group="site 100", shape="hexagon", status="ok"),
        Node("br1", lines=["system 192.0.2.11", "20.18.1"], group="site 200", status="warn"),
        Node("br2", group="site 200", status="down"),
        Node("internet", shape="cloud"),
    ]
    g.edges += [
        Edge("br1", "hub1", label="mpls / mpls"),
        Edge("br2", "hub1", label="biz-internet", status="down"),
        Edge("br1", "ghost", label="outside the narrowed view"),
    ]
    return g


def test_mermaid_groups_shapes_status_and_down_edges() -> None:
    out = to_mermaid(fabric())
    lines = out.splitlines()
    assert lines[0] == "graph LR"
    assert '  subgraph g0["site 100"]' in lines
    assert '    n0{{"hub1<br/>system 192.0.2.1"}}:::ok' in lines
    assert '    n1["br1<br/>system 192.0.2.11<br/>20.18.1"]:::warn' in lines
    assert '  n3>"internet"]' in lines  # ungrouped, cloud shape
    assert '  n1 ---|"mpls / mpls"| n0' in lines
    assert '  n2 -.-|"biz-internet"| n0' in lines
    assert "ghost" not in out
    assert "  classDef down fill:#f8cecc,stroke:#b85450" in lines
    assert "  linkStyle 1 stroke:#b85450,stroke-width:2px" in lines
    assert '  notes["dashed = BFD down"]' in lines
    assert out.count("  end") == 2


def test_mermaid_escapes_labels() -> None:
    assert mermaid_text('a"b<c>|d#e&\nf') == "a#quot;b#lt;c#gt;#124;d#35;e#amp; f"
    g = Graph(nodes=[Node("x", label='evil"]; click x call alert()')], direction="TB")
    out = to_mermaid(g)
    assert out.startswith("graph TB\n")
    assert '"]; click' not in out


def test_drawio_is_valid_xml_with_containers_and_styles() -> None:
    out = to_drawio(fabric(), host="cat-sdwan-mcp-server")
    root = ET.fromstring(out)
    assert root.get("host") == "cat-sdwan-mcp-server"
    assert root.find("diagram").get("name") == "SD-WAN fabric"  # type: ignore[union-attr]
    cells = {c.get("id"): c for c in root.iter("mxCell")}
    assert cells["g0"].get("value") == "site 100"
    assert "shape=hexagon" in cells["n0"].get("style", "")
    assert "fillColor=#fff2cc" in cells["n1"].get("style", "")
    assert cells["n1"].get("value") == "<b>br1</b><br>system 192.0.2.11<br>20.18.1"
    down = cells["e1"]
    assert down.get("source") == "n2" and down.get("target") == "n0"
    assert "dashed=1" in down.get("style", "")
    assert "e2" not in cells  # edge to a node not in the graph
    assert "notes" in cells


def test_drawio_escapes_and_strips_control_characters() -> None:
    g = Graph(title="t\x01", nodes=[Node("a", label="<script>&\x07")])
    root = ET.fromstring(to_drawio(g))
    cell = next(c for c in root.iter("mxCell") if c.get("id") == "n0")
    assert cell.get("value") == "<b>&lt;script&gt;&amp;</b>"


def test_layout_keeps_positions_or_uses_group_columns() -> None:
    placed = Graph(nodes=[Node("a", x=-100, y=50), Node("b", x=100, y=250)])
    pos = layout(placed)
    assert pos["a"][0] == 80 and pos["b"][0] == 280
    grid = layout(fabric())
    assert grid["br1"][0] == grid["br2"][0] > grid["hub1"][0]
    assert grid["br2"][1] > grid["br1"][1]


def test_from_dict_and_render_dispatch() -> None:
    data = {
        "title": "VPN hub-and-spoke",
        "direction": "sideways",  # invalid -> LR
        "nodes": [
            {"id": "hub", "shape": "hexagon", "group": "hubs", "lines": ["198.51.100.1", None, ""]},
            {"id": "spoke1", "status": "bogus", "x": "10", "y": 5},
        ],
        "edges": [{"a": "spoke1", "b": "hub", "label": "IKEv2", "status": "down", "directed": True}],
    }
    g = Graph.from_dict(data)
    assert g.direction == "LR"
    assert g.nodes[0].lines == ["198.51.100.1"]
    assert g.nodes[1].status is None and g.nodes[1].x == 10.0
    mer = render(data, "mermaid")
    assert '  n1 -.->|"IKEv2"| n0' in mer
    assert render(g, "drawio").startswith("<mxfile")
    with pytest.raises(InvalidRequest, match="Unknown diagram format"):
        render(g, "svg")


def test_checks_refuse_duplicates_and_huge_graphs() -> None:
    with pytest.raises(InvalidRequest, match="Duplicate node id"):
        to_mermaid(Graph(nodes=[Node("a"), Node("a")]))
    with pytest.raises(InvalidRequest, match="too many"):
        to_drawio(Graph(nodes=[Node(str(i)) for i in range(MAX_NODES + 1)]))


def test_diagram_result_fences_mermaid_only() -> None:
    g = Graph(nodes=[Node("a")])
    mer = diagram_result(g, "mermaid").content[0].text  # type: ignore[union-attr]
    assert mer.startswith("```mermaid\ngraph LR\n") and mer.endswith("```\n")
    dio = diagram_result(g, "drawio").content[0].text  # type: ignore[union-attr]
    assert dio.startswith("<mxfile")


def test_short_if() -> None:
    assert short_if("GigabitEthernet1/0/1") == "Gi1/0/1"
    assert short_if("TenGigabitEthernet1/1/1") == "Te1/1/1"
    assert short_if("Tunnel100") == "Tu100"
    assert short_if("ge0/0") == "ge0/0"
