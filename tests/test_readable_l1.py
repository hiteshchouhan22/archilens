"""
L1 stays readable on large projects: line width instead of an "N imports" label on
every arrow, and at most diagrams.l1_max_edges dependencies drawn, with every one
still listed below the diagram. Django 6.1 has 17 modules and 109 dependencies.
"""

from __future__ import annotations

import re

from archilens.config import ArchiLensConfig
from archilens.generators.base import omitted_note, select_l1_edges, stroke_width
from archilens.generators.d2 import D2Generator
from archilens.generators.mermaid import generate_module_architecture
from archilens.generators.plantuml import PlantUMLGenerator
from archilens.models import ArchEdge, ArchNode, ArchSnapshot, DiagramLevel, EdgeType, NodeType


def _edge(src: str, tgt: str, weight: int) -> ArchEdge:
    return ArchEdge(source=f"module:{src}", target=f"module:{tgt}", edge_type=EdgeType.DEPENDENCY,
                    label=f"{weight} imports", weight=weight)


def _snapshot(edges: list[ArchEdge]) -> ArchSnapshot:
    names = sorted({e.source for e in edges} | {e.target for e in edges})
    nodes = [ArchNode(id=n, name=n.removeprefix("module:"), node_type=NodeType.MODULE, level=DiagramLevel.MODULE)
             for n in names]
    return ArchSnapshot(project_name="P", nodes=nodes, edges=edges)


def _dense() -> list[ArchEdge]:
    """Every pair among 12 hubs (66 dependencies), plus a leaf with one light dependency."""
    hubs = [f"m{i:02d}" for i in range(12)]
    edges = [_edge(a, b, 1 + (i * 7 + j) % 9) for i, a in enumerate(hubs) for j, b in enumerate(hubs) if i < j]
    return edges + [_edge("leaf", "m00", 1)]


class TestSelection:
    def test_small_graphs_are_drawn_whole(self):
        edges = [_edge("a", "b", 1), _edge("b", "c", 5)]
        assert select_l1_edges(edges, 40) == sorted(edges, key=lambda e: (e.source, e.target))
        assert omitted_note(2, 2) == ""

    def test_large_graphs_keep_the_heaviest_and_every_module(self):
        edges = _dense()
        shown = select_l1_edges(edges, 40)
        assert len(shown) == 40
        # The leaf's only dependency weighs 1, the minimum, yet it is drawn.
        assert _edge("leaf", "m00", 1) in shown
        modules = {e.source for e in edges} | {e.target for e in edges}
        assert {e.source for e in shown} | {e.target for e in shown} == modules
        # Apart from the spine, nothing lighter is drawn while something heavier is left out.
        left_out = [e for e in edges if e not in shown]
        assert max(e.weight for e in left_out) <= sorted(e.weight for e in shown)[-30]

    def test_zero_draws_everything(self):
        assert len(select_l1_edges(_dense(), 0)) == len(_dense())

    def test_stable_order(self):
        assert select_l1_edges(_dense(), 40) == select_l1_edges(list(reversed(_dense())), 40)

    def test_width_is_relative_to_the_heaviest_on_a_log_scale(self):
        assert [stroke_width(w, 5) for w in (1, 2, 3, 5)] == [1.0, 2.5, 3.0, 4.0]
        # Django-sized weights still spread out instead of all hitting the maximum
        assert [stroke_width(w, 400) for w in (1, 8, 20, 60, 150, 400)] == [1.0, 2.0, 2.5, 3.0, 3.5, 4.0]
        assert stroke_width(1, 1) == stroke_width(3, 1) == 1.0  # all single imports: all thin


class TestMermaid:
    def test_no_import_labels_and_widths_by_link_style(self):
        out = generate_module_architecture(_snapshot([_edge("a", "b", 1), _edge("b", "c", 9)]))
        assert "imports|" not in out
        assert "    module_a --> module_b" in out
        assert "    linkStyle 0 stroke-width:1px" in out
        assert "    linkStyle 1 stroke-width:4px" in out
        assert "<sub>Line width shows the number of imports.</sub>" in out

    def test_dense_graph_says_what_it_omits_and_lists_everything(self):
        edges = _dense()
        out = generate_module_architecture(_snapshot(edges), 40)
        fence = out.split("```mermaid")[1].split("```")[0]
        assert len(re.findall(r"^    \S+ --> \S+$", fence, flags=re.MULTILINE)) == 40
        assert f"Showing 40 of {len(edges)} dependencies: the heaviest, and at least one for every module." in out
        assert f"<details><summary>All {len(edges)} dependencies</summary>" in out
        table = out.split("| From | To | Imports |")[1]
        assert table.count("\n| ") == len(edges)

    def test_limit_comes_from_the_config(self):
        from archilens.generators.mermaid import MermaidGenerator

        config = ArchiLensConfig()
        config.diagrams.l1_max_edges = 0
        out = MermaidGenerator(config).module_architecture(_snapshot(_dense()))
        assert "Showing" not in out


class TestOtherFormats:
    def test_d2_and_plantuml_use_widths_and_the_same_selection(self):
        snap = _snapshot(_dense())
        d2 = D2Generator(ArchiLensConfig()).module_architecture(snap)
        puml = PlantUMLGenerator(ArchiLensConfig()).module_architecture(snap)
        assert len(re.findall(r"^\S+ -> \S+: \{style\.stroke-width: [\d.]+\}$", d2, flags=re.MULTILINE)) == 40
        assert len(re.findall(r"^\S+ -\[thickness=\d\]-> \S+$", puml, flags=re.MULTILINE)) == 40
        assert "# Showing 40 of 67 dependencies" in d2
        assert "' Showing 40 of 67 dependencies" in puml
        assert "imports" not in d2.split("direction: right")[1]
