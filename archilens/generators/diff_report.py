"""
The architecture-diff report: the Markdown (with Mermaid) that ``archilens diff``
prints and the GitHub Action posts as the PR comment.

Renders an ``ArchDiff`` computed by ``archilens.analyzers.diff``, including the
optional AI review (``ArchDiff.review``), whose text is treated as untrusted.
"""

from __future__ import annotations

import re

from archilens.generators.base import shorten
from archilens.generators.mermaid import escape_label, sanitize_id, sequence_diagram
from archilens.models import ArchDiff, DiffReview, EdgeType

_MODULE = "module:"

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
        "    classDef added fill:#1a7f37,stroke:#4ac26b,color:#fff",
        "    classDef removed fill:#cf222e,stroke:#ff8182,color:#fff,stroke-dasharray:4 3",
        "    classDef resized fill:#9a6700,stroke:#d4a72c,color:#fff",
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
    credit = f"Written by {_ai_text(review.model, 60)} from the changes below, which are the source of truth."
    if review.usage:
        credit += f" AI usage: {review.usage.text()}."
    lines += [f"<sub>{credit}</sub>", ""]
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
