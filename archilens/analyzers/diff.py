"""
Architecture diff engine.

Compares two ArchSnapshots and produces a structured diff
showing what changed architecturally. Used for PR comments
and CI/CD drift detection.
"""

from __future__ import annotations

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
    - Added/removed modules (nodes)
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
    diff.summary = (
        f"Architecture changes: {added} added, {removed} removed, "
        f"{modified} modified across {len(diff.entries)} total changes."
    )

    return diff


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


def format_diff_as_markdown(diff: ArchDiff) -> str:
    """Format the architecture diff as a GitHub PR comment (emoji-free for terminal safety)."""
    lines = [
        "## ArchiLens: Architecture Diff",
        "",
        f"Comparing `{diff.base_ref}` -> `{diff.head_ref}`",
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

        symbol = {"added": "+", "removed": "-", "modified": "~"}
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
            f"already present on <code>{diff.base_ref}</code></summary>"
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
