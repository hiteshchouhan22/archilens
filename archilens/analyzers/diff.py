"""
Architecture diff engine.

Compares two ArchSnapshots and produces a structured diff
showing what changed architecturally. Used for PR comments
and CI/CD drift detection.
"""

from __future__ import annotations

import re

from archilens.config import CIRule
from archilens.generators.base import shorten
from archilens.generators.mermaid import escape_label, sanitize_id, sequence_diagram
from archilens.models import ArchDiff, ArchNode, ArchSnapshot, DiffEntry, DiffReview, EdgeType, NodeType
from archilens.rules import evaluate_rules

_MODULE = "module:"

# Flag a node / edge as "modified" when its LOC / import count changes by this fraction.
_LOC_CHANGE_THRESHOLD = 0.2
_WEIGHT_CHANGE_THRESHOLD = 0.2


def compute_diff(base: ArchSnapshot, head: ArchSnapshot) -> ArchDiff:
    """
    Compare two architecture snapshots and return the diff.

    Detects:
    - Added/removed modules and classes (nodes)
    - Classes that moved to another file, reported once per file move
    - Added/removed dependencies (edges)
    - Added/removed process flows
    - Changes in dependency weight
    """
    diff = ArchDiff(base_ref=base.git_ref, head_ref=head.git_ref)

    # --- Node diff ---
    # Every loop below iterates in sorted order: set order changes between Python
    # processes (hash randomisation), which would reshuffle the PR comment on each run.
    base_nodes = {n.id: n for n in base.nodes}
    head_nodes = {n.id: n for n in head.nodes}
    base_node_ids = set(base_nodes)
    head_node_ids = set(head_nodes)

    # A class whose file was renamed or moved would otherwise appear as one
    # removal plus one addition; pair them and report one entry per file move.
    moved_from, moved_to = _match_moved_classes(
        [base_nodes[i] for i in base_node_ids - head_node_ids],
        [head_nodes[i] for i in head_node_ids - base_node_ids],
    )
    for (old_file, new_file), names in sorted(_group_moves(moved_from, moved_to, base_nodes, head_nodes).items()):
        diff.entries.append(
            DiffEntry(
                change_type="moved",
                entity_type="node",
                entity_id=f"{old_file}->{new_file}",
                description=(
                    f"Moved {len(names)} class{'es' if len(names) > 1 else ''} from {old_file} "
                    f"to {new_file}: {', '.join(names)}"
                ),
                severity="info",
            )
        )
    base_node_ids -= set(moved_from)
    head_node_ids -= set(moved_to)

    for node_id in sorted(head_node_ids - base_node_ids):
        node = head_nodes[node_id]
        diff.entries.append(
            DiffEntry(
                change_type="added",
                entity_type="node",
                entity_id=node_id,
                description=f"New {_describe(node)} added",
                severity="info",
            )
        )

    for node_id in sorted(base_node_ids - head_node_ids):
        node = base_nodes[node_id]
        diff.entries.append(
            DiffEntry(
                change_type="removed",
                entity_type="node",
                entity_id=node_id,
                description=f"{_describe(node)[0].upper()}{_describe(node)[1:]} removed",
                severity="warning" if node.node_type == NodeType.MODULE else "info",
            )
        )

    # --- Modified nodes (same node, LOC changed by >20%) ---
    for node_id in sorted(base_node_ids & head_node_ids):
        base_node = base_nodes[node_id]
        head_node = head_nodes[node_id]
        bloc = base_node.lines_of_code
        hloc = head_node.lines_of_code
        if bloc > 0 and abs(hloc - bloc) / bloc >= _LOC_CHANGE_THRESHOLD:
            direction = "grew" if hloc > bloc else "shrank"
            diff.entries.append(
                DiffEntry(
                    change_type="modified",
                    entity_type="node",
                    entity_id=node_id,
                    description=f"Module {base_node.name} {direction}: {bloc:,} -> {hloc:,} LOC",
                    severity="info",
                )
            )

    # --- Edge diff ---
    base_edge_keys = {(e.source, e.target, e.edge_type) for e in base.edges}
    head_edge_keys = {(e.source, e.target, e.edge_type) for e in head.edges}

    for key in sorted(head_edge_keys - base_edge_keys, key=_edge_sort_key):
        src, tgt, etype = key
        diff.entries.append(
            DiffEntry(
                change_type="added",
                entity_type="edge",
                entity_id=f"{src}->{tgt}",
                description=f"New dependency: {src} -> {tgt} ({etype.value})",
                severity="info",
                source=src,
                target=tgt,
                edge_type=etype.value,
            )
        )

    for key in sorted(base_edge_keys - head_edge_keys, key=_edge_sort_key):
        src, tgt, etype = key
        diff.entries.append(
            DiffEntry(
                change_type="removed",
                entity_type="edge",
                entity_id=f"{src}->{tgt}",
                description=f"Dependency removed: {src} -> {tgt} ({etype.value})",
                severity="info",
                source=src,
                target=tgt,
                edge_type=etype.value,
            )
        )

    # --- Modified edges (same key, changed weight — coupling increased/decreased) ---
    base_edge_weights = {(e.source, e.target, e.edge_type): e.weight for e in base.edges}
    head_edge_weights = {(e.source, e.target, e.edge_type): e.weight for e in head.edges}

    for key in sorted(base_edge_keys & head_edge_keys, key=_edge_sort_key):
        bw = base_edge_weights[key]
        hw = head_edge_weights[key]
        if bw > 0 and abs(hw - bw) / bw >= _WEIGHT_CHANGE_THRESHOLD:
            src, tgt, etype = key
            direction = "increased" if hw > bw else "decreased"
            diff.entries.append(
                DiffEntry(
                    change_type="modified",
                    entity_type="edge",
                    entity_id=f"{src}->{tgt}",
                    description=f"Coupling {direction}: {src} -> {tgt} ({bw} -> {hw} imports)",
                    severity="info",
                    source=src,
                    target=tgt,
                    edge_type=etype.value,
                    before=bw,
                    after=hw,
                )
            )

    # --- Flow diff ---
    base_flow_ids = {f.id for f in base.flows}
    head_flow_ids = {f.id for f in head.flows}

    head_flows = {f.id: f for f in head.flows}
    for flow_id in sorted(head_flow_ids - base_flow_ids):
        flow = head_flows[flow_id]
        diff.entries.append(
            DiffEntry(
                change_type="added",
                entity_type="flow",
                entity_id=flow_id,
                description=f"New process flow: {flow.name}",
                severity="info",
            )
        )

    # Build summary
    added = sum(1 for e in diff.entries if e.change_type == "added")
    removed = sum(1 for e in diff.entries if e.change_type == "removed")
    modified = sum(1 for e in diff.entries if e.change_type == "modified")
    moved = sum(1 for e in diff.entries if e.change_type == "moved")
    diff.summary = (
        f"Architecture changes: {added} added, {removed} removed, "
        f"{modified} modified{f', {moved} moved' if moved else ''} across {len(diff.entries)} total changes."
    )

    return diff


def _match_moved_classes(removed: list[ArchNode], added: list[ArchNode]) -> tuple[list[str], list[str]]:
    """
    Pair removed and added class nodes that are the same class in a new file.

    A pair is made only when the name (and node type) is unambiguous: exactly one
    removed and one added node carry it. Returns parallel lists of
    (base node id, head node id).
    """

    def by_name(nodes: list[ArchNode]) -> dict[tuple[str, str], list[ArchNode]]:
        groups: dict[tuple[str, str], list[ArchNode]] = {}
        for node in nodes:
            if node.node_type != NodeType.MODULE and node.file_path:
                groups.setdefault((node.node_type.value, node.name), []).append(node)
        return groups

    old, new = by_name(removed), by_name(added)
    pairs = sorted(
        (old[key][0].id, new[key][0].id)
        for key in old.keys() & new.keys()
        if len(old[key]) == 1 and len(new[key]) == 1 and old[key][0].file_path != new[key][0].file_path
    )
    return [a for a, _ in pairs], [b for _, b in pairs]


def _group_moves(
    moved_from: list[str], moved_to: list[str], base_nodes: dict[str, ArchNode], head_nodes: dict[str, ArchNode]
) -> dict[tuple[str, str], list[str]]:
    """(old file, new file) -> sorted class names that moved between them."""
    groups: dict[tuple[str, str], list[str]] = {}
    for old_id, new_id in zip(moved_from, moved_to, strict=True):
        old, new = base_nodes[old_id], head_nodes[new_id]
        groups.setdefault((old.file_path or "", new.file_path or ""), []).append(old.name)
    return {key: sorted(names) for key, names in groups.items()}


def _describe(node: ArchNode) -> str:
    """Human label for a node in a diff entry, e.g. "module Orders" or "class Foo in app/orders"."""
    if node.node_type == NodeType.MODULE:
        return f"module {node.name}"
    where = f" in {node.parent.removeprefix('module:')}" if node.parent else ""
    return f"{node.node_type.value} {node.name}{where}"


def _edge_sort_key(key: tuple[str, str, EdgeType]) -> tuple[str, str, str]:
    src, tgt, etype = key
    return (src, tgt, etype.value)


def check_rules(
    diff: ArchDiff,
    head: ArchSnapshot,
    rules: list[CIRule],
    base: ArchSnapshot | None = None,
) -> list[str]:
    """
    Evaluate *rules* on *head* and record the result on *diff*.

    With *base*, violations that base already had are kept apart in
    ``diff.existing_violations``; only the rest are "new" (``diff.violations``).
    That lets a team adopt a rule on a codebase that breaks it today and still
    fail pull requests that make things worse. Without *base*, every violation
    is new.

    Returns the messages of the new violations (also ``diff.rule_violations``).
    """
    head_violations = evaluate_rules(head, rules)
    base_keys = {v.key for v in evaluate_rules(base, rules)} if base is not None else set()
    diff.violations = [v for v in head_violations if v.key not in base_keys]
    diff.existing_violations = [v for v in head_violations if v.key in base_keys]
    diff.rule_violations = [v.text() for v in diff.violations]
    return diff.rule_violations


# Above this many modules or dependencies, the change diagram is skipped as unreadable.
_DIAGRAM_MAX_MODULES = 30
_DIAGRAM_MAX_EDGES = 40


def _change_diagram(diff: ArchDiff) -> list[str]:
    """
    Mermaid diagram of the modules and module dependencies this diff changes.

    Modules are coloured as added, removed or resized (LOC changed); other modules
    appear only as endpoints of a changed dependency. Dependencies are drawn as new
    (thick), removed (dotted) or with a changed import count. A new dependency that
    breaks a forbidden-dependency rule is labelled with the rule.
    """
    module_status: dict[str, str] = {}
    for entry in diff.entries:
        if entry.entity_type == "node" and entry.entity_id.startswith(_MODULE):
            module_status[entry.entity_id.removeprefix(_MODULE)] = entry.change_type
    edges = [
        e
        for e in diff.entries
        if e.entity_type == "edge"
        and e.edge_type == EdgeType.DEPENDENCY.value
        and (e.source or "").startswith(_MODULE)
        and (e.target or "").startswith(_MODULE)
    ]
    if not module_status and not edges:
        return []

    modules = set(module_status)
    for e in edges:
        modules |= {(e.source or "").removeprefix(_MODULE), (e.target or "").removeprefix(_MODULE)}
    if len(modules) > _DIAGRAM_MAX_MODULES or len(edges) > _DIAGRAM_MAX_EDGES:
        return [
            "### Changed modules",
            "",
            f"*{len(modules)} modules and {len(edges)} dependencies changed; too many to draw. See the table below.*",
            "",
        ]

    violated: dict[tuple[str, str], list[str]] = {}
    for v in diff.violations:
        if v.rule_type == "forbidden-dependency" and len(v.modules) == 2:
            violated.setdefault((v.modules[0], v.modules[1]), []).append(v.rule)

    styles = {"added": "added", "removed": "removed", "modified": "resized"}
    lines = [
        "### Changed modules",
        "",
        "```mermaid",
        "flowchart LR",
        "    classDef added fill:#2ea043,stroke:#1a7f37,color:#fff",
        "    classDef removed fill:#da3633,stroke:#a40e26,color:#fff,stroke-dasharray:4 3",
        "    classDef resized fill:#9e6a03,stroke:#7d4e00,color:#fff",
        "    classDef context fill:#f6f8fa,stroke:#8c959f,color:#24292f",
    ]
    for module in sorted(modules):
        style = styles.get(module_status.get(module, ""), "context")
        lines.append(f'    {_diagram_id(module)}["{escape_label(module)}"]:::{style}')
    # Arrows are coloured with linkStyle (by index) rather than labelled, so a large
    # change stays readable; only rule violations and import counts get text.
    link_styles: dict[str, list[int]] = {"added": [], "removed": [], "modified": [], "violation": []}
    for index, e in enumerate(sorted(edges, key=lambda x: (x.source or "", x.target or ""))):
        src = (e.source or "").removeprefix(_MODULE)
        tgt = (e.target or "").removeprefix(_MODULE)
        a, b = _diagram_id(src), _diagram_id(tgt)
        if e.change_type == "added":
            rules = violated.get((src, tgt))
            if rules:
                lines.append(f'    {a} ==>|"{escape_label("violates " + ", ".join(rules))}"| {b}')
                link_styles["violation"].append(index)
            else:
                lines.append(f"    {a} ==> {b}")
                link_styles["added"].append(index)
        elif e.change_type == "removed":
            lines.append(f"    {a} -.-> {b}")
            link_styles["removed"].append(index)
        else:
            lines.append(f'    {a} -->|"{e.before} to {e.after} imports"| {b}')
            link_styles["modified"].append(index)
    colours = {
        "added": "stroke:#2ea043,stroke-width:2px",
        "removed": "stroke:#cf222e,stroke-width:1.5px",
        "modified": "stroke:#bf8700,stroke-width:1.5px",
        "violation": "stroke:#cf222e,stroke-width:3.5px",
    }
    for kind, indices in link_styles.items():
        if indices:
            lines.append(f"    linkStyle {','.join(map(str, indices))} {colours[kind]}")
    lines += [
        "```",
        "",
        "<sub>Modules: green new, red removed, amber size changed by 20% or more. Dependencies: green new, "
        "red dotted removed, amber import count changed, thick red breaks a rule.</sub>",
        "",
    ]
    return lines


def _diagram_id(module: str) -> str:
    """Mermaid node ID for a module; the prefix keeps names like "end" or "graph" from clashing with keywords."""
    return "m_" + sanitize_id(module)


def _short_ref(ref: str) -> str:
    """Abbreviate a full commit SHA (as CI passes them) to 8 characters; leave branch and tag names alone."""
    return ref[:8] if re.fullmatch(r"[0-9a-f]{40}", ref) else ref


def _ai_text(text: str, width: int) -> str:
    """
    Model-written text made safe for a PR comment: one line of at most *width*
    characters, no HTML or images, no @-mentions that would notify anyone, and no
    leading "[" (the GitHub Action counts violations as lines starting "- [").
    """
    text = shorten(text, width).replace("<", "&lt;").replace(">", "&gt;").replace("![", "!\\[")
    text = re.sub(r"@(?=\w)", "@​", text)
    return "\\" + text if text.startswith("[") else text


def _review_summary(review: DiffReview) -> list[str]:
    """The AI summary and highlights that open the report."""
    lines = ["### Summary", "", _ai_text(review.summary, 1200), ""]
    if review.highlights:
        lines += ["**Worth a look**", ""]
        lines += [f"- {_ai_text(point, 300)}" for point in review.highlights]
        lines.append("")
    lines += [
        f"<sub>Written by {_ai_text(review.model, 60)} from the changes below, which are the source of truth.</sub>",
        "",
    ]
    return lines


def format_diff_as_markdown(diff: ArchDiff) -> str:
    """Format the architecture diff as a GitHub PR comment (emoji-free for terminal safety)."""
    lines = [
        "## ArchiLens: Architecture Diff",
        "",
        f"Comparing `{_short_ref(diff.base_ref)}` -> `{_short_ref(diff.head_ref)}`",
        "",
    ]
    if diff.review:
        lines += _review_summary(diff.review)
    lines += [diff.summary, ""]
    module_descriptions = diff.review.module_descriptions if diff.review else {}

    if diff.has_breaking_changes:
        lines.append("> **WARNING: Breaking architectural changes detected!**")
        lines.append("")

    lines += _change_diagram(diff)

    if diff.entries:
        lines.append("### Changes")
        lines.append("")
        lines.append("| Type | Change | Details | Severity |")
        lines.append("|------|--------|---------|----------|")

        symbol = {"added": "+", "removed": "-", "modified": "~", "moved": ">"}
        for entry in diff.entries:
            icon = symbol.get(entry.change_type, "?")
            details = entry.description
            if entry.entity_id in module_descriptions:
                details += ": " + _ai_text(module_descriptions[entry.entity_id], 200).replace("|", "\\|")
            lines.append(f"| {entry.entity_type} | [{icon}] {entry.change_type} | {details} | {entry.severity} |")
        lines.append("")

    if diff.review and diff.review.flows:
        lines.append("### New process flows")
        lines.append("")
        for flow in diff.review.flows:
            title = _ai_text(flow.name, 120) + (f": {_ai_text(flow.trigger, 100)}" if flow.trigger else "")
            lines.append(f"<details><summary>{title} (<code>{_ai_text(flow.entry_point or '', 200)}</code>)</summary>")
            lines.append("")
            if flow.description:
                lines.append(_ai_text(flow.description, 400))
                lines.append("")
            lines += sequence_diagram(flow)
            lines.append("")
            lines.append("</details>")
            lines.append("")

    if diff.rule_violations or diff.existing_violations:
        lines.append("### Rule Violations")
        lines.append("")
        for v in diff.rule_violations:
            lines.append(f"- {v}")
        if not diff.rule_violations:
            lines.append("No new rule violations.")
        lines.append("")
    if diff.existing_violations:
        # Not "- [" at line start, so tools counting new violations skip these.
        lines.append(
            f"<details><summary>{len(diff.existing_violations)} existing violation(s), "
            f"already present on <code>{_short_ref(diff.base_ref)}</code></summary>"
        )
        lines.append("")
        for violation in diff.existing_violations:
            lines.append(f"- (existing) {violation.text()}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append("*Generated by [ArchiLens](https://github.com/saurabh-oss/archilens)*")

    return "\n".join(lines)
