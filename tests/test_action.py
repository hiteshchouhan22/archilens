"""
The composite GitHub Action: the Marketplace copy at the repository root and the
github_action/ copy must stay identical, and the root copy must meet the
Marketplace's metadata rules.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
FEATHER_COLORS = {"white", "black", "yellow", "blue", "green", "orange", "red", "purple", "gray-dark"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_root_and_subfolder_actions_are_identical():
    root = (ROOT / "action.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
    sub = (ROOT / "github_action" / "action.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert root == sub, "edit action.yml and copy it to github_action/action.yml"


def test_marketplace_metadata():
    action = _load(ROOT / "action.yml")
    assert action["name"] and action["name"].lower() != "archilens"  # a GitHub user named ARCHILENS exists
    assert 0 < len(action["description"]) <= 125
    assert action["branding"]["color"] in FEATHER_COLORS
    assert action["branding"]["icon"]
    assert action["runs"]["using"] == "composite"


def test_only_one_action_metadata_file_at_the_root():
    assert not (ROOT / "action.yaml").exists()


def test_dogfood_workflow_uses_the_root_action():
    workflow = _load(ROOT / ".github" / "workflows" / "archilens.yml")
    steps = workflow["jobs"]["archilens"]["steps"]
    assert any(step.get("uses") == "./" for step in steps)
