"""
Abstract base for diagram generators.

Each format (Mermaid, D2, PlantUML) implements this interface so that
engine.py can dispatch by ``config.diagrams.format`` without knowing the
details of any specific syntax.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

from archilens.config import ArchiLensConfig
from archilens.models import ArchSnapshot, ProcessFlow


class DiagramGenerator(ABC):
    """Base class for all diagram format generators."""

    #: File extension for generated files (without leading dot)
    extension: str = "md"

    def __init__(self, config: ArchiLensConfig) -> None:
        self.config = config

    def generate_all(
        self,
        snapshot: ArchSnapshot,
        output_dir: str | Path,
    ) -> list[Path]:
        """
        Generate all configured diagram levels and write to *output_dir*.

        Returns a list of created file paths.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[Path] = []
        used: set[str] = set()  # file stems already written, so same-named modules don't overwrite

        for level_cfg in self.config.diagrams.levels:
            if not level_cfg.get("enabled", True):
                continue

            level = level_cfg["level"]

            if level == 0:
                path = output_dir / f"L0_system_context.{self.extension}"
                path.write_text(self.system_context(snapshot), encoding="utf-8")
                generated.append(path)

            elif level == 1:
                path = output_dir / f"L1_module_architecture.{self.extension}"
                path.write_text(self.module_architecture(snapshot), encoding="utf-8")
                generated.append(path)

            elif level == 2:
                modules = level_cfg.get("modules", [])
                module_nodes = snapshot.get_module_nodes()
                targets = (
                    [n for n in module_nodes if any(n.file_path and n.file_path.startswith(m) for m in modules)]
                    if modules
                    else module_nodes
                )
                for mod_node in targets:
                    stem = _unique(_slug(mod_node.name), _slug(mod_node.file_path or mod_node.id), used)
                    path = output_dir / f"L2_{stem}_components.{self.extension}"
                    content = self.component_detail(snapshot, mod_node.id)
                    if content:
                        path.write_text(content, encoding="utf-8")
                        generated.append(path)

            elif level == 3:
                max_flows = level_cfg.get("max_flows", 10)
                for flow in snapshot.flows[:max_flows]:
                    stem = _unique(_slug(flow.name), _slug(flow.id), used)
                    path = output_dir / f"L3_{stem}.{self.extension}"
                    path.write_text(self.process_flow(flow), encoding="utf-8")
                    generated.append(path)

        index_path = output_dir / "INDEX.md"
        index_path.write_text(self.index(snapshot, generated), encoding="utf-8")
        generated.append(index_path)

        return generated

    @abstractmethod
    def system_context(self, snapshot: ArchSnapshot) -> str:
        """Generate L0 system context diagram."""

    @abstractmethod
    def module_architecture(self, snapshot: ArchSnapshot) -> str:
        """Generate L1 module architecture diagram."""

    @abstractmethod
    def component_detail(self, snapshot: ArchSnapshot, module_id: str) -> str | None:
        """Generate L2 component detail diagram for a module."""

    @abstractmethod
    def process_flow(self, flow: ProcessFlow) -> str:
        """Generate L3 process flow / sequence diagram."""

    def index(self, snapshot: ArchSnapshot, generated: list[Path]) -> str:
        """Generate an INDEX.md linking all diagrams (format-agnostic)."""
        lines = [
            f"# ArchiLens — {snapshot.project_name}",
            "",
            f"*Generated from `{snapshot.git_ref or 'HEAD'}`*",
            f"*Format: {self.__class__.__name__.replace('Generator', '')}*",
            "",
            "## Architecture Diagrams",
            "",
            "| Level | Diagram |",
            "|-------|---------|",
        ]
        for f in generated:
            if f.name == "INDEX.md":
                continue
            name = f.stem.replace("_", " ")
            level = name[:2] if name[:2] in ("L0", "L1", "L2", "L3") else "--"
            lines.append(f"| {level} | [{name}]({f.name}) |")

        lines.append("")
        if snapshot.capability_map:
            lines.append("## Business Capabilities")
            lines.append("")
            for cap_name, mod_ids in snapshot.capability_map.items():
                lines.append(f"### {cap_name}")
                for mod_id in mod_ids:
                    node = next((n for n in snapshot.nodes if n.id == mod_id), None)
                    if node:
                        lines.append(f"- {node.name}")
                lines.append("")

        return "\n".join(lines)


def _slug(text: str) -> str:
    """
    A file-name stem that is valid on every OS and safe in a Markdown link:
    lower-case ASCII letters, digits, ``.``, ``-`` and ``_`` only.
    """
    return re.sub(r"[^a-z0-9._-]+", "_", text.lower()).strip("._") or "diagram"


def _unique(preferred: str, fallback: str, used: set[str]) -> str:
    """*preferred*, or *fallback* (then a counter) when another diagram already took it."""
    for candidate in (preferred, f"{preferred}_{fallback}"):
        if candidate not in used:
            used.add(candidate)
            return candidate
    n = 2
    while f"{preferred}_{n}" in used:
        n += 1
    used.add(f"{preferred}_{n}")
    return f"{preferred}_{n}"


def get_generator(config: ArchiLensConfig) -> DiagramGenerator:
    """Instantiate the correct generator for ``config.diagrams.format``."""
    fmt = (config.diagrams.format or "mermaid").lower()

    if fmt == "d2":
        from archilens.generators.d2 import D2Generator

        return D2Generator(config)
    elif fmt in ("plantuml", "puml"):
        from archilens.generators.plantuml import PlantUMLGenerator

        return PlantUMLGenerator(config)
    else:
        from archilens.generators.mermaid import MermaidGenerator

        return MermaidGenerator(config)
