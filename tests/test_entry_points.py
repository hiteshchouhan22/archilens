"""
Entry-point detection (process flows work with no configuration), word-boundary
shortening of AI summaries in diagrams, and descriptions in INDEX.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from archilens.analyzers.discovery import discover_files
from archilens.analyzers.entry_points import MAX_DETECTED
from archilens.config import ArchiLensConfig
from archilens.generators.base import shorten
from archilens.generators.mermaid import MermaidGenerator, generate_module_architecture
from archilens.models import ArchNode, ArchSnapshot, DiagramLevel, NodeType, ProcessFlow


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def _entries(repo: Path, config: ArchiLensConfig | None = None) -> dict[str, str]:
    files, _ = discover_files(repo, config or ArchiLensConfig())
    return {f.relative_path: f.entry_type for f in files if f.is_entry_point}


class TestManifests:
    def test_pyproject_console_scripts(self, tmp_path):
        repo = _repo(
            tmp_path,
            {
                "pyproject.toml": '[project]\nname = "x"\n\n[project.scripts]\nshop = "shop.cli:main"\n',
                "shop/__init__.py": "",
                "shop/cli.py": "def main(): ...\n",
                "shop/__main__.py": "from shop.cli import main\nmain()\n",
            },
        )
        # __main__.py is the usual wrapper around the console script, so it is not a second flow.
        assert _entries(repo) == {"shop/cli.py": "cli"}

    def test_poetry_scripts_src_layout_and_subdirectory(self, tmp_path):
        repo = _repo(
            tmp_path,
            {
                "services/api/pyproject.toml": '[tool.poetry.scripts]\napi = "api.run:start"\n',
                "services/api/src/api/__init__.py": "",
                "services/api/src/api/run.py": "def start(): ...\n",
            },
        )
        assert _entries(repo) == {"services/api/src/api/run.py": "cli"}

    def test_package_json_bin_and_main(self, tmp_path):
        repo = _repo(
            tmp_path,
            {
                "package.json": json.dumps({"bin": {"tool": "./bin/tool.js"}, "main": "lib/api.js"}),
                "bin/tool.js": "",
                "lib/api.js": "",
            },
        )
        assert _entries(repo) == {"bin/tool.js": "cli", "lib/api.js": "application_entry"}

    def test_bin_pointing_at_build_output_is_ignored(self, tmp_path):
        repo = _repo(
            tmp_path,
            {"package.json": json.dumps({"bin": "dist/cli.js"}), "src/util.ts": ""},
        )
        assert _entries(repo) == {}

    def test_broken_manifests_are_ignored(self, tmp_path):
        repo = _repo(tmp_path, {"package.json": "{not json", "pyproject.toml": "[[[", "lib/a.py": ""})
        assert _entries(repo) == {}


class TestConventions:
    def test_common_entry_file_names(self, tmp_path):
        repo = _repo(
            tmp_path,
            {
                "cmd/server/main.go": "package main\n",
                "src/main/java/com/acme/ShopApplication.java": "class ShopApplication {}\n",
                "web/app.py": "",
            },
        )
        assert _entries(repo) == {
            "cmd/server/main.go": "application_entry",
            "src/main/java/com/acme/ShopApplication.java": "application_entry",
            "web/app.py": "http_handler",
        }

    def test_js_names_only_at_a_package_root_or_its_src(self, tmp_path):
        repo = _repo(
            tmp_path,
            {
                "package.json": "{}",
                "src/main.ts": "",
                "src/components/index.ts": "",  # a barrel file, not an entry point
                "packages/api/package.json": "{}",
                "packages/api/server.js": "",
            },
        )
        assert _entries(repo) == {"src/main.ts": "application_entry", "packages/api/server.js": "http_handler"}

    def test_tests_are_never_entry_points(self, tmp_path):
        repo = _repo(tmp_path, {"tests/app.py": "", "app_test.go": "", "main_test.go": ""})
        assert _entries(repo) == {}


class TestLimitsAndConfig:
    def test_capped_with_manifest_entries_first(self, tmp_path):
        files = {f"svc{i}/main.go": "package main\n" for i in range(MAX_DETECTED + 3)}
        files["package.json"] = json.dumps({"bin": "cli.js"})
        files["cli.js"] = ""
        entries = _entries(_repo(tmp_path, files))
        assert len(entries) == MAX_DETECTED
        assert next(iter(entries)) == "cli.js"

    def test_configured_entry_points_replace_detection(self, tmp_path):
        repo = _repo(tmp_path, {"main.go": "package main\n", "handlers/orders.go": ""})
        config = ArchiLensConfig()
        config.analysis.entry_points = [{"pattern": "handlers/*.go", "type": "http_handler"}]
        assert _entries(repo, config) == {"handlers/orders.go": "http_handler"}


class TestShorten:
    def test_short_text_is_unchanged_but_flattened(self):
        assert shorten("Parses\n  files.", 60) == "Parses files."

    def test_cuts_at_a_word_boundary_with_an_ellipsis(self):
        text = "Top-level package providing the command-line interface and the version"
        result = shorten(text, 60)
        assert len(result) <= 60
        assert result.endswith("…")
        assert text.startswith(result[:-1])
        assert result[:-1].split()[-1] in text.split()  # no word cut in half

    def test_one_long_word_is_cut_hard(self):
        assert shorten("x" * 100, 10) == "x" * 9 + "…"


def _snapshot() -> ArchSnapshot:
    module = ArchNode(
        id="module:orders",
        name="Orders",
        node_type=NodeType.MODULE,
        level=DiagramLevel.MODULE,
        file_path="orders",
        ai_summary="Handles order placement | cancellation and refunds for customers across every storefront",
    )
    snapshot = ArchSnapshot(project_name="Shop", nodes=[module])
    snapshot.capability_map = {"Commerce": ["module:orders"]}
    snapshot.flows = [ProcessFlow(id="flow:a", name="Checkout", description="Customer pays for a cart.")]
    return snapshot


class TestDiagramText:
    def test_l1_label_never_cuts_a_word(self):
        diagram = generate_module_architecture(_snapshot())
        assert "<small>Handles order placement | cancellation and refunds for…</small>" in diagram

    def test_index_describes_each_diagram(self, tmp_path):
        MermaidGenerator(ArchiLensConfig()).generate_all(_snapshot(), tmp_path)
        index = (tmp_path / "INDEX.md").read_text(encoding="utf-8")
        assert "| Level | Diagram | Description |" in index
        assert "| Modules, grouped by business capability, and the dependencies between them |" in index
        assert "| Customer pays for a cart. |" in index

    def test_index_description_cannot_break_the_table(self):
        index = MermaidGenerator(ArchiLensConfig()).index(
            _snapshot(), [Path("L2_orders_components.md")], {"L2_orders_components.md": "Orders | refunds"}
        )
        assert "| Orders \\| refunds |" in index
