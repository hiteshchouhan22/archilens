"""
A config section whose contents are all commented out is YAML null; it must
mean "use the defaults", not crash.
"""

from __future__ import annotations

from pathlib import Path

from archilens.config import AIConfig, AnalysisConfig, DiagramConfig, load_config


def test_sections_left_empty_use_the_defaults(tmp_path: Path):
    (tmp_path / ".archilens.yml").write_text(
        "\n".join(
            f"{section}:\n  # everything commented out"
            for section in ("project", "analysis", "diagrams", "ai", "ci", "capabilities",
                            "external_systems", "evolution")
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.analysis.exclude == AnalysisConfig().exclude
    assert config.diagrams.levels == DiagramConfig().levels
    assert config.ai.features == AIConfig().features
    assert config.ci.rules == [] and config.capabilities == [] and config.baseline_refs == []


def test_empty_keys_use_the_defaults_but_empty_lists_stay_empty(tmp_path: Path):
    (tmp_path / ".archilens.yml").write_text(
        "analysis:\n  exclude:\n  languages:\n  entry_points:\nci:\n  rules:\n"
        "diagrams:\n  levels: []\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.analysis.exclude == AnalysisConfig().exclude  # null: the default
    assert config.analysis.languages == [] and config.analysis.entry_points == []
    assert config.diagrams.levels == []  # an explicit empty list is respected
