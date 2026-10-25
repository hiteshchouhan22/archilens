"""
Architecture rules from ``ci.rules`` in .archilens.yml, evaluated against a snapshot.

Rule types:

- ``forbidden-dependency``: no module matching the ``from`` side may depend on a
  module matching the ``to`` side. Each side selects modules by path glob
  (``from`` / ``to``) and/or by business capability (``from_capability`` /
  ``to_capability``); when both are given for a side, both must match.
- ``max-fan-out``: a module may depend on at most ``threshold`` other modules.
- ``no-cycles``: modules must not depend on each other in a cycle.

Only module-level dependency edges are considered. Each violation has a stable
``key`` so ``archilens diff`` can tell violations a pull request introduces
from ones that were already on the base branch.
"""

from __future__ import annotations

from collections import defaultdict

import networkx as nx

from archilens.config import CIRule
from archilens.models import ArchSnapshot, EdgeType, RuleViolation
from archilens.utils import module_matches

_MODULE = "module:"


def evaluate_rules(snapshot: ArchSnapshot, rules: list[CIRule]) -> list[RuleViolation]:
    """Return every violation of *rules* in *snapshot*, sorted by key."""
    capability = {n.id.removeprefix(_MODULE): n.capability for n in snapshot.get_module_nodes()}
    edges = sorted(
        {
            (e.source.removeprefix(_MODULE), e.target.removeprefix(_MODULE))
            for e in snapshot.edges
            if e.edge_type == EdgeType.DEPENDENCY and e.source.startswith(_MODULE) and e.target.startswith(_MODULE)
        }
    )

    violations: list[RuleViolation] = []
    for rule in rules:
        if rule.kind == "forbidden-dependency":
            for src, tgt in edges:
                if forbids(rule, src, tgt, capability.get(src), capability.get(tgt)):
                    violations.append(_forbidden(rule, src, tgt, capability.get(src), capability.get(tgt)))
        elif rule.kind == "max-fan-out" and rule.threshold:
            targets: dict[str, set[str]] = defaultdict(set)
            for src, tgt in edges:
                targets[src].add(tgt)
            for src, tgts in sorted(targets.items()):
                if len(tgts) > rule.threshold:
                    violations.append(
                        RuleViolation(
                            rule=rule.name,
                            rule_type=rule.kind,
                            action=rule.action,
                            message=f"Module {src} has {len(tgts)} dependencies (threshold: {rule.threshold})",
                            key=f"{rule.name}|{src}",
                            modules=[src],
                        )
                    )
        elif rule.kind == "no-cycles":
            graph = nx.DiGraph(edges)
            for component in nx.strongly_connected_components(graph):
                if len(component) > 1:
                    members = sorted(component)
                    violations.append(
                        RuleViolation(
                            rule=rule.name,
                            rule_type=rule.kind,
                            action=rule.action,
                            message=f"Dependency cycle between {', '.join(members)}",
                            key=f"{rule.name}|{','.join(members)}",
                            modules=members,
                        )
                    )
    return sorted(violations, key=lambda v: v.key)


def forbids(rule: CIRule, src: str, tgt: str, src_capability: str | None, tgt_capability: str | None) -> bool:
    """True if forbidden-dependency *rule* forbids module *src* depending on module *tgt*."""
    if rule.kind != "forbidden-dependency" or src == tgt:
        return False
    return _side_matches(src, src_capability, rule.from_pattern, rule.from_capability) and _side_matches(
        tgt, tgt_capability, rule.to_pattern, rule.to_capability
    )


def _side_matches(module: str, capability: str | None, pattern: str | None, wanted_capability: str | None) -> bool:
    if pattern is None and wanted_capability is None:
        return False
    if pattern is not None and not module_matches(module, pattern):
        return False
    return wanted_capability is None or capability == wanted_capability


def _forbidden(rule: CIRule, src: str, tgt: str, src_cap: str | None, tgt_cap: str | None) -> RuleViolation:
    message = f"Forbidden dependency from {src} to {tgt}"
    if rule.from_capability or rule.to_capability:
        message += f" ({src_cap or 'no capability'} -> {tgt_cap or 'no capability'})"
    return RuleViolation(
        rule=rule.name,
        rule_type=rule.kind,
        action=rule.action,
        message=message,
        key=f"{rule.name}|{src}|{tgt}",
        modules=[src, tgt],
    )
