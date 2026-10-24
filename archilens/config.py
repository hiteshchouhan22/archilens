"""
Configuration loader for ArchiLens.

Reads and validates the .archilens.yml configuration file from
the repository root, providing sensible defaults for any
omitted settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AnalysisConfig:
    languages: list[str] = field(default_factory=lambda: [])
    entry_points: list[dict[str, str]] = field(default_factory=list)
    exclude: list[str] = field(
        default_factory=lambda: [
            "**/node_modules/**",
            "**/__pycache__/**",
            "**/venv/**",
            "**/.venv/**",
            "**/site-packages/**",
            "**/.tox/**",
            "**/.git/**",
            "**/dist/**",
            "**/build/**",
            "**/target/**",
            "**/vendor/**",
            "**/.next/**",
            "**/coverage/**",
            "**/*.min.js",
            "**/*.test.*",
            "**/*.spec.*",
        ]
    )
    max_depth: int = 5  # reserved, not used yet
    # Directory levels below a source root (src/, src/main/java, go.mod dir, ...)
    # that name an L1 module. JS/TS workspace packages are always one module each.
    module_depth: int = 2


@dataclass
class DiagramConfig:
    output_dir: str = ".archilens/diagrams"
    format: str = "mermaid"
    levels: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"level": 0, "name": "System Context", "enabled": True},
            {"level": 1, "name": "Module Architecture", "enabled": True},
            {"level": 2, "name": "Component Detail", "enabled": True},
            {"level": 3, "name": "Process Flows", "enabled": True},
        ]
    )
    theme: str = "default"


DEFAULT_MODEL = "claude-opus-5-5"
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class AIConfig:
    provider: str = "anthropic"
    model: str = DEFAULT_MODEL
    # Reasoning effort for Claude 5-generation models: low | medium | high | xhigh | max.
    # None leaves the model's default. Ignored for older models and other providers.
    effort: str | None = "low"
    features: dict[str, bool] = field(
        default_factory=lambda: {
            "process_flow_inference": True,
            "node_annotations": True,
            "capability_suggestions": True,
            "pattern_detection": True,
            "module_summaries": True,
        }
    )


@dataclass
class CIRule:
    name: str
    action: str = "warn"
    from_pattern: str | None = None
    to_pattern: str | None = None
    threshold: int | None = None


@dataclass
class CIConfig:
    drift_detection: bool = True
    pr_comments: bool = True
    rules: list[CIRule] = field(default_factory=list)


@dataclass
class Capability:
    name: str
    description: str = ""
    modules: list[str] = field(default_factory=list)


@dataclass
class ExternalSystem:
    name: str
    type: str = "external_api"
    description: str = ""


@dataclass
class ArchiLensConfig:
    """Root configuration object."""

    project_name: str = "Unnamed Project"
    project_description: str = ""
    project_type: str = "monolith"

    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    diagrams: DiagramConfig = field(default_factory=DiagramConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    ci: CIConfig = field(default_factory=CIConfig)

    capabilities: list[Capability] = field(default_factory=list)
    external_systems: list[ExternalSystem] = field(default_factory=list)

    evolution_enabled: bool = True
    baseline_refs: list[str] = field(default_factory=lambda: ["main"])


class ConfigError(Exception):
    """Raised when .archilens.yml is malformed or contains invalid values."""


def load_config(repo_path: str | Path) -> ArchiLensConfig:
    """
    Load ArchiLens configuration from a repository.

    Looks for .archilens.yml or .archilens.yaml in the repo root.
    Returns default config if no file is found.

    Raises:
        ConfigError: If the config file exists but cannot be parsed.
    """
    repo_path = Path(repo_path)

    for filename in [".archilens.yml", ".archilens.yaml"]:
        config_path = repo_path / filename
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(
                    f"Could not parse {config_path.name}: {exc}\nCheck for tabs instead of spaces, or missing colons."
                ) from exc

            if not isinstance(raw, dict):
                raise ConfigError(
                    f"{config_path.name} must be a YAML mapping (key: value pairs), got {type(raw).__name__}."
                )

            try:
                return _parse_config(raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise ConfigError(
                    f"Invalid value in {config_path.name}: {exc}\n"
                    "Run 'python -m archilens init' to see the expected structure."
                ) from exc

    # No config file found — use defaults with auto-detection
    return ArchiLensConfig()


def _parse_module_depth(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"analysis.module_depth must be a positive integer, got {value!r}")
    return value


def _parse_effort(value: Any) -> str | None:
    """Validate ``ai.effort``; None (YAML null) means "use the model's default"."""
    if value is None:
        return None
    if value not in _EFFORT_LEVELS:
        raise ValueError(f"ai.effort must be one of {', '.join(_EFFORT_LEVELS)} or null, got {value!r}")
    return str(value)


def _parse_config(raw: dict[str, Any]) -> ArchiLensConfig:
    """Parse raw YAML dict into typed config."""
    project = raw.get("project", {})
    analysis_raw = raw.get("analysis", {})
    diagrams_raw = raw.get("diagrams", {})
    ai_raw = raw.get("ai", {})
    ci_raw = raw.get("ci", {})

    config = ArchiLensConfig(
        project_name=project.get("name", "Unnamed Project"),
        project_description=project.get("description", ""),
        project_type=project.get("type", "monolith"),
        analysis=AnalysisConfig(
            languages=analysis_raw.get("languages", []),
            entry_points=analysis_raw.get("entry_points", []),
            exclude=analysis_raw.get("exclude", AnalysisConfig().exclude),
            max_depth=analysis_raw.get("max_depth", 5),
            module_depth=_parse_module_depth(analysis_raw.get("module_depth", 2)),
        ),
        diagrams=DiagramConfig(
            output_dir=diagrams_raw.get("output_dir", ".archilens/diagrams"),
            format=diagrams_raw.get("format", "mermaid"),
            levels=diagrams_raw.get("levels", DiagramConfig().levels),
            theme=diagrams_raw.get("theme", "default"),
        ),
        ai=AIConfig(
            provider=ai_raw.get("provider", "anthropic"),
            model=ai_raw.get("model", DEFAULT_MODEL),
            effort=_parse_effort(ai_raw.get("effort", "low")),
            features=ai_raw.get("features", AIConfig().features),
        ),
        ci=CIConfig(
            drift_detection=ci_raw.get("drift_detection", True),
            pr_comments=ci_raw.get("pr_comments", True),
            rules=[
                CIRule(
                    name=r["name"],
                    action=r.get("action", "warn"),
                    from_pattern=r.get("from"),
                    to_pattern=r.get("to"),
                    threshold=r.get("threshold"),
                )
                for r in ci_raw.get("rules", [])
            ],
        ),
        capabilities=[
            Capability(
                name=c["name"],
                description=c.get("description", ""),
                modules=c.get("modules", []),
            )
            for c in raw.get("capabilities", [])
        ],
        external_systems=[
            ExternalSystem(
                name=s["name"],
                type=s.get("type", "external_api"),
                description=s.get("description", ""),
            )
            for s in raw.get("external_systems", [])
        ],
        evolution_enabled=raw.get("evolution", {}).get("enabled", True),
        baseline_refs=raw.get("evolution", {}).get("baseline_refs", ["main"]),
    )

    return config
