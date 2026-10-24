"""
Tests for the 0.2.2 hardening batch:
  - discovery: .gitignore via git ls-files, default excludes, test directories, POSIX paths
  - metrics: cycle membership without enumerating cycles
  - reproducible output: sorted diff entries, no timestamp in INDEX.md
  - label escaping in Mermaid and D2
  - viewer: loopback by default, debugger only on loopback, escaped HTML
  - MCP: snapshot rebuilt when the work tree, config or version changes
  - AI: request shape per model generation, credential preflight
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archilens.config import AIConfig, ArchiLensConfig, ConfigError, load_config
from archilens.models import (
    ArchEdge,
    ArchNode,
    ArchSnapshot,
    DiagramLevel,
    EdgeType,
    NodeType,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _module(id_: str, name: str, loc: int = 100) -> ArchNode:
    return ArchNode(id=id_, name=name, node_type=NodeType.MODULE, level=DiagramLevel.MODULE, lines_of_code=loc)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def _discover(self, repo: Path) -> list[str]:
        from archilens.analyzers.discovery import discover_files

        files, _ = discover_files(repo, ArchiLensConfig())
        return [f.relative_path for f in files]

    def test_skips_dot_venv_and_dependency_dirs_without_git(self, tmp_path):
        _write(
            tmp_path,
            {
                "app/orders/service.py": "x = 1\n",
                ".venv/Lib/site-packages/requests/api.py": "x = 1\n",
                "node_modules/left-pad/index.js": "x = 1\n",
                "target/classes/Gen.java": "class Gen {}\n",
                "web/static/app.min.js": "x=1\n",
            },
        )
        assert self._discover(tmp_path) == ["app/orders/service.py"]

    @needs_git
    def test_respects_gitignore(self, tmp_path):
        _write(
            tmp_path,
            {
                ".gitignore": "generated/\n",
                "app/main.py": "x = 1\n",
                "generated/models_pb2.py": "x = 1\n",
                "untracked_but_not_ignored.py": "x = 1\n",
            },
        )
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "add", "app/main.py", ".gitignore")
        found = self._discover(tmp_path)
        assert "generated/models_pb2.py" not in found
        assert "app/main.py" in found
        assert "untracked_but_not_ignored.py" in found

    def test_paths_are_posix_and_sorted(self, tmp_path):
        _write(tmp_path, {"b/z.py": "", "a/y/x.py": "", "a/b.py": ""})
        found = self._discover(tmp_path)
        assert found == sorted(found)
        assert all("\\" not in f for f in found)

    @pytest.mark.parametrize(
        "path",
        ["tests/test_app.py", "tests/__init__.py", "test/acceptance/app.js", "src/test/java/FooIT.java",
         "pkg/testdata/sample.go", "spec/models/user.rb"],
    )
    def test_files_in_test_directories_are_tests(self, path):
        from archilens.analyzers.discovery import _is_test_file

        assert _is_test_file(path)

    @pytest.mark.parametrize("path", ["src/orders/service.py", "lib/router.js", "app/contest/entry.py"])
    def test_ordinary_files_are_not_tests(self, path):
        from archilens.analyzers.discovery import _is_test_file

        assert not _is_test_file(path)

    def test_top_level_tests_package_is_not_a_module(self, tmp_path):
        from archilens.engine import analyze_repository

        _write(tmp_path, {"app/core.py": "class Core: pass\n", "tests/__init__.py": "", "tests/conftest.py": ""})
        snap = analyze_repository(tmp_path, ArchiLensConfig(), use_ai=False)
        assert [n.id for n in snap.get_module_nodes()] == ["module:app"]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestCycleMetrics:
    def test_cycle_membership_from_strongly_connected_components(self):
        import networkx as nx

        from archilens.analyzers.dependencies import compute_metrics

        g = nx.DiGraph([("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("e", "e")])
        metrics = compute_metrics(g)
        in_cycle = sorted(n for n, m in metrics.items() if m.get("in_cycle"))
        assert in_cycle == ["a", "b", "c", "e"]

    def test_dense_graph_finishes_quickly(self):
        import time

        import networkx as nx

        from archilens.analyzers.dependencies import compute_metrics

        # A complete digraph on 40 nodes has an astronomical number of simple cycles.
        g = nx.complete_graph(40, create_using=nx.DiGraph)
        start = time.perf_counter()
        metrics = compute_metrics(g)
        assert time.perf_counter() - start < 10
        assert all(m["in_cycle"] for m in metrics.values())


# ---------------------------------------------------------------------------
# Reproducible output
# ---------------------------------------------------------------------------


class TestReproducibleOutput:
    def test_diff_labels_class_nodes_as_classes(self):
        from archilens.analyzers.diff import compute_diff

        mod = _module("module:app", "App")
        cls = ArchNode(
            id="class:app/x.py:Foo", name="Foo", node_type=NodeType.CLASS,
            level=DiagramLevel.COMPONENT, parent="module:app",
        )
        diff = compute_diff(ArchSnapshot(project_name="t", nodes=[mod]), ArchSnapshot(project_name="t", nodes=[mod, cls]))
        assert [e.description for e in diff.entries] == ["New class Foo in app added"]

    def test_module_removal_is_a_warning_class_removal_is_info(self):
        from archilens.analyzers.diff import compute_diff

        mod = _module("module:old", "Old")
        cls = ArchNode(id="class:a.py:A", name="A", node_type=NodeType.CLASS, level=DiagramLevel.COMPONENT)
        diff = compute_diff(ArchSnapshot(project_name="t", nodes=[mod, cls]), ArchSnapshot(project_name="t"))
        severities = {e.entity_id: e.severity for e in diff.entries}
        assert severities == {"module:old": "warning", "class:a.py:A": "info"}

    def test_diff_entries_are_sorted(self):
        from archilens.analyzers.diff import compute_diff

        names = ["zeta", "alpha", "mid", "beta"]
        head = ArchSnapshot(
            project_name="t",
            nodes=[_module(f"module:{n}", n) for n in names],
            edges=[ArchEdge(source=f"module:{n}", target="module:alpha", edge_type=EdgeType.DEPENDENCY) for n in names],
        )
        diff = compute_diff(ArchSnapshot(project_name="t"), head)
        node_ids = [e.entity_id for e in diff.entries if e.entity_type == "node"]
        edge_ids = [e.entity_id for e in diff.entries if e.entity_type == "edge"]
        assert node_ids == sorted(node_ids)
        assert edge_ids == sorted(edge_ids)

    def test_index_has_no_timestamp(self, tmp_path):
        from archilens.generators.base import get_generator

        snap = ArchSnapshot(project_name="T", git_ref="HEAD", timestamp="2026-10-24T10:00:00+00:00",
                            nodes=[_module("module:app", "App")])
        for fmt in ("mermaid", "d2", "plantuml"):
            config = ArchiLensConfig()
            config.diagrams.format = fmt
            out = tmp_path / fmt
            get_generator(config).generate_all(snap, out)
            assert "2026-10-24" not in (out / "INDEX.md").read_text(encoding="utf-8")

    def test_same_repo_gives_identical_diagrams(self, tmp_path):
        from archilens.engine import analyze_repository, generate_diagrams

        repo = _write(tmp_path / "repo", {
            "app/orders/service.py": "from app.payments.gateway import charge\nclass OrderService: pass\n",
            "app/payments/gateway.py": "def charge(): pass\n",
        })
        outputs = []
        for run in ("one", "two"):
            snap = analyze_repository(repo, ArchiLensConfig(), use_ai=False)
            files = generate_diagrams(snap, ArchiLensConfig(), tmp_path / run)
            outputs.append({f.name: f.read_text(encoding="utf-8") for f in files})
        assert outputs[0] == outputs[1]


# ---------------------------------------------------------------------------
# Label escaping
# ---------------------------------------------------------------------------


class TestLabelEscaping:
    def _snap(self) -> ArchSnapshot:
        node = _module("module:app", "App")
        node.capability = "Core"
        node.ai_summary = 'Handles "checkout" & <b>pay|ments</b>\nacross lines'
        return ArchSnapshot(project_name="t", nodes=[node], capability_map={"Core": ["module:app"]})

    def test_mermaid_l1_label_is_escaped(self):
        from archilens.generators.mermaid import generate_module_architecture

        out = generate_module_architecture(self._snap())
        node_line = next(line for line in out.splitlines() if "module_app[" in line)
        assert node_line.count('"') == 2  # only the label delimiters
        assert "<b>" not in node_line
        assert "#quot;checkout#quot;" in node_line
        table_row = next(line for line in out.splitlines() if line.startswith("| App |"))
        assert "pay\\|ments" in table_row
        assert "\n" not in table_row

    def test_d2_tooltip_is_escaped(self):
        from archilens.generators.d2 import D2Generator

        out = D2Generator(ArchiLensConfig()).module_architecture(self._snap())
        tooltip = next(line.strip() for line in out.splitlines() if "tooltip:" in line)
        assert tooltip.startswith('tooltip: "') and tooltip.endswith('"')
        assert '\\"checkout\\"' in tooltip


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------


class TestViewerSafety:
    def test_binds_to_loopback_by_default(self, tmp_path):
        app = MagicMock()
        with patch("archilens.viewer.app.create_app", return_value=app):
            result = CliRunner().invoke(__import__("archilens.cli", fromlist=["main"]).main, ["serve", "--repo", str(tmp_path)])
        assert result.exit_code == 0
        assert app.run.call_args.kwargs["host"] == "127.0.0.1"

    def test_debug_refused_on_non_loopback_host(self, tmp_path):
        from archilens.cli import main

        app = MagicMock()
        with patch("archilens.viewer.app.create_app", return_value=app):
            result = CliRunner().invoke(main, ["serve", "--repo", str(tmp_path), "--host", "0.0.0.0", "--debug"])
        assert result.exit_code == 1
        app.run.assert_not_called()

    def test_page_escapes_untrusted_text(self, tmp_path):
        pytest.importorskip("flask")
        from archilens.viewer.app import create_app

        _write(tmp_path, {"app/core.py": "class Core: pass\n"})
        page = create_app(tmp_path).test_client().get("/").get_data(as_text=True)
        assert "function esc(" in page
        assert "securityLevel: 'strict'" in page
        for unsafe in ("+ e.message +", "+ d.label +", "+ d.summary", "data.mermaid +"):
            assert unsafe not in page


# ---------------------------------------------------------------------------
# MCP freshness
# ---------------------------------------------------------------------------


@needs_git
class TestMcpFreshness:
    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        pytest.importorskip("mcp.server.fastmcp", reason="requires the 'mcp' extra")
        from archilens.mcp import cache, server

        repo = _write(tmp_path / "repo", {"app/orders/service.py": "class OrderService:\n    pass\n"})
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "init")
        monkeypatch.setattr(cache, "_CACHE_DB", tmp_path / "cache.db")
        monkeypatch.setattr(server, "_snapshot", None)
        monkeypatch.setattr(server, "_loaded_key", None)
        server.init_server(repo)
        return repo

    def test_uncommitted_edit_rebuilds_snapshot(self, repo):
        from archilens.mcp import server

        first = server._ensure_loaded()
        assert server._ensure_loaded() is first  # unchanged tree: reuse
        _write(repo, {"app/payments/gateway.py": "class Gateway:\n    pass\n"})
        second = server._ensure_loaded()
        assert second is not first
        assert "module:app/payments" in {n.id for n in second.get_module_nodes()}

    def test_key_covers_config_and_version(self, repo, monkeypatch):
        import archilens
        from archilens.mcp import server

        _write(repo, {".archilens.yml": "project:\n  name: A\n"})
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "config")
        key_a, clean = server._state_key(repo)
        assert clean

        _write(repo, {".archilens.yml": "project:\n  name: B\n"})
        key_b, _ = server._state_key(repo)
        assert key_b != key_a

        monkeypatch.setattr(archilens, "__version__", "9.9.9")
        assert "v9.9.9" in server._state_key(repo)[0]

    def test_dirty_state_is_not_written_to_disk_cache(self, repo):
        from archilens.mcp import cache, server

        _write(repo, {"app/new.py": "x = 1\n"})
        key, clean = server._state_key(repo)
        assert not clean
        server._ensure_loaded()
        assert cache.load_snapshot(repo, key) is None


# ---------------------------------------------------------------------------
# AI client
# ---------------------------------------------------------------------------


def _response(text: str = '{"summary": "s", "responsibility": "r", "suggested_capability": "c"}',
              stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )


class TestAIClient:
    @pytest.mark.parametrize(
        ("model", "current"),
        [("claude-opus-5-5", True), ("claude-haiku-5-5", True), ("claude-sonnet-5", True),
         ("claude-sonnet-4-20250514", False), ("claude-opus-4-8", False), ("claude-sonnet-4-6", False),
         ("claude-3-5-haiku-latest", False)],
    )
    def test_model_generation(self, model, current):
        from archilens.ai import _is_current_generation

        assert _is_current_generation(model) is current

    def test_current_model_uses_structured_output_not_forced_tool(self):
        from archilens.ai import _SUMMARY_TOOL, AIClient

        client = AIClient(AIConfig(model="claude-haiku-5-5", effort="low"))
        sdk = MagicMock()
        sdk.messages.create.return_value = _response()
        client._client = sdk

        result = client.complete_with_tool("prompt", _SUMMARY_TOOL)

        assert result == {"summary": "s", "responsibility": "r", "suggested_capability": "c"}
        kwargs = sdk.messages.create.call_args.kwargs
        assert "tool_choice" not in kwargs and "tools" not in kwargs
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        assert kwargs["output_config"]["effort"] == "low"
        assert kwargs["max_tokens"] >= 8000  # thinking shares the budget

    def test_opus_5_5_requests_server_side_fallback(self):
        from archilens.ai import _SUMMARY_TOOL, AIClient

        client = AIClient(AIConfig(model="claude-opus-5-5"))
        sdk = MagicMock()
        sdk.beta.messages.create.return_value = _response()
        client._client = sdk

        assert client.complete_with_tool("prompt", _SUMMARY_TOOL) is not None
        kwargs = sdk.beta.messages.create.call_args.kwargs
        assert kwargs["fallbacks"] == "default"
        assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
        sdk.messages.create.assert_not_called()

    def test_legacy_model_keeps_forced_tool(self):
        from archilens.ai import _SUMMARY_TOOL, AIClient

        client = AIClient(AIConfig(model="claude-sonnet-4-20250514"))
        sdk = MagicMock()
        sdk.messages.create.return_value = SimpleNamespace(
            stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", input={"summary": "s"})]
        )
        client._client = sdk

        assert client.complete_with_tool("prompt", _SUMMARY_TOOL) == {"summary": "s"}
        kwargs = sdk.messages.create.call_args.kwargs
        assert kwargs["tool_choice"] == {"type": "tool", "name": "record_summary"}
        assert "output_config" not in kwargs

    def test_refusal_returns_none(self):
        from archilens.ai import _SUMMARY_TOOL, AIClient

        client = AIClient(AIConfig(model="claude-haiku-5-5"))
        sdk = MagicMock()
        sdk.messages.create.return_value = _response(text="", stop_reason="refusal")
        client._client = sdk
        assert client.complete_with_tool("prompt", _SUMMARY_TOOL) is None

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (TypeError("Could not resolve authentication method"), "no Anthropic credentials found"),
            ("not_found", "was not found"),
        ],
    )
    def test_check_ready_reports_reason(self, error, reason):
        import anthropic
        import httpx2

        from archilens.ai import AIClient

        if error == "not_found":
            request = httpx2.Request("GET", "https://api.anthropic.com/v1/models/x")
            error = anthropic.NotFoundError("missing", response=httpx2.Response(404, request=request), body=None)
        client = AIClient(AIConfig(model="claude-nope"))
        sdk = MagicMock()
        sdk.models.retrieve.side_effect = error
        client._client = sdk
        assert reason in client.check_ready()

    def test_engine_skips_ai_with_one_warning_when_not_ready(self, tmp_path, caplog):
        from archilens.engine import analyze_repository

        _write(tmp_path, {"a/x.py": "class X: pass\n", "b/y.py": "class Y: pass\n"})
        with patch("archilens.ai.AIClient.check_ready", return_value="no Anthropic credentials found"), \
             patch("archilens.engine.generate_module_summaries") as summaries, \
             caplog.at_level(logging.WARNING):
            snap = analyze_repository(tmp_path, ArchiLensConfig(), use_ai=True)
        summaries.assert_not_called()
        assert len(snap.get_module_nodes()) == 2
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "Skipping AI analysis" in warnings[0].getMessage()

    def test_patterns_accept_objects_and_strings(self):
        from archilens.ai import detect_patterns
        from archilens.models import ArchPattern

        client = MagicMock()
        client.complete_with_tool.return_value = {
            "patterns": [{"name": "layered", "reason": "r"}, "repository", {"name": "bogus", "reason": "x"}]
        }
        snap = ArchSnapshot(project_name="t", nodes=[_module("module:a", "A")])
        assert detect_patterns(snap, "tree", client) == [ArchPattern.LAYERED, ArchPattern.REPOSITORY]

    def test_schemas_are_valid_for_structured_outputs(self):
        """Every object must be closed and list all its properties as required."""
        from archilens.ai import _FLOW_TOOL, _PATTERN_TOOL, _SUMMARY_TOOL

        def check(schema: dict) -> None:
            if schema.get("type") == "object":
                assert schema.get("additionalProperties") is False
                assert set(schema["required"]) == set(schema["properties"])
                for sub in schema["properties"].values():
                    check(sub)
            if schema.get("type") == "array":
                check(schema["items"])

        for tool in (_FLOW_TOOL, _SUMMARY_TOOL, _PATTERN_TOOL):
            check(tool["input_schema"])


class TestConfig:
    def test_default_model_and_effort(self, tmp_path):
        cfg = load_config(tmp_path)
        assert cfg.ai.model == "claude-opus-5-5"
        assert cfg.ai.effort == "low"

    def test_effort_null_means_model_default(self, tmp_path):
        (tmp_path / ".archilens.yml").write_text("ai:\n  effort: null\n", encoding="utf-8")
        assert load_config(tmp_path).ai.effort is None

    def test_invalid_effort_is_a_config_error(self, tmp_path):
        (tmp_path / ".archilens.yml").write_text("ai:\n  effort: turbo\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="ai.effort"):
            load_config(tmp_path)
