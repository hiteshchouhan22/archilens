"""
Architecture diff engine.

Compares two ArchSnapshots and produces a structured diff
showing what changed architecturally. Used for PR comments
and CI/CD drift detection.
"""

from __future__ import annotations

import re

from archilens.config import CIRule
from archilens.models import ArchDiff, ArchNode, ArchSnapshot, DiffEntry, EdgeType, NodeType
from archilens.rules import evaluate_rules

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


def _short_ref(ref: str) -> str:
    """Abbreviate a full commit SHA (as CI passes them) to 8 characters; leave branch and tag names alone."""
    return ref[:8] if re.fullmatch(r"[0-9a-f]{40}", ref) else ref


def format_diff_as_markdown(diff: ArchDiff) -> str:
    """Format the architecture diff as a GitHub PR comment (emoji-free for terminal safety)."""
    lines = [
        "## ArchiLens: Architecture Diff",
        "",
        f"Comparing `{_short_ref(diff.base_ref)}` -> `{_short_ref(diff.head_ref)}`",
        "",
        diff.summary,
        "",
    ]

    if diff.has_breaking_changes:
        lines.append("> **WARNING: Breaking architectural changes detected!**")
        lines.append("")

    if diff.entries:
        lines.append("### Changes")
        lines.append("")
        lines.append("| Type | Change | Details | Severity |")
        lines.append("|------|--------|---------|----------|")

        symbol = {"added": "+", "removed": "-", "modified": "~", "moved": ">"}
        for entry in diff.entries:
            icon = symbol.get(entry.change_type, "?")
            lines.append(
                f"| {entry.entity_type} | [{icon}] {entry.change_type} | {entry.description} | {entry.severity} |"
            )
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
