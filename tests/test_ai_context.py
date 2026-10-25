"""
Tests for AI context and caching: signature outlines, content-addressed caching
of AI answers, capability suggestions, and entry-point types reaching the flow
prompt. The SDK client is mocked; nothing calls a real API.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from archilens.ai import _SUMMARY_TOOL, AIClient, generate_module_summaries, module_outline
from archilens.ai.cache import AICache
from archilens.config import AIConfig, ArchiLensConfig, Capability
from archilens.models import ArchNode, ArchSnapshot, DiagramLevel, NodeType


def _response(text: str) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def _client(tmp_path: Path, model: str = "claude-haiku-5-5") -> tuple[AIClient, MagicMock]:
    client = AIClient(AIConfig(model=model), cache=AICache(tmp_path / "ai_cache.db"))
    sdk = MagicMock()
    sdk.messages.create.return_value = _response(
        '{"summary": "Handles orders.", "responsibility": "order lifecycle", "suggested_capability": "Order Management"}'
    )
    client._client = sdk
    return client, sdk


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestCache:
    def test_identical_request_is_answered_from_cache(self, tmp_path):
        client, sdk = _client(tmp_path)
        first = client.complete_with_tool("prompt A", _SUMMARY_TOOL)
        second = client.complete_with_tool("prompt A", _SUMMARY_TOOL)
        assert first == second == {
            "summary": "Handles orders.", "responsibility": "order lifecycle", "suggested_capability": "Order Management"
        }
        assert sdk.messages.create.call_count == 1

    def test_a_cache_survives_a_new_client(self, tmp_path):
        client, sdk = _client(tmp_path)
        client.complete_with_tool("prompt A", _SUMMARY_TOOL)
        fresh, fresh_sdk = _client(tmp_path)
        assert fresh.complete_with_tool("prompt A", _SUMMARY_TOOL) is not None
        fresh_sdk.messages.create.assert_not_called()

    @pytest.mark.parametrize("change", ["prompt", "model", "effort"])
    def test_any_input_change_misses_the_cache(self, tmp_path, change):
        client, sdk = _client(tmp_path)
        client.complete_with_tool("prompt A", _SUMMARY_TOOL)
        if change == "prompt":
            client.complete_with_tool("prompt B", _SUMMARY_TOOL)
        elif change == "model":
            client.config.model = "claude-opus-5-5"
            sdk.beta.messages.create.return_value = _response('{"summary": "x", "responsibility": "y", "suggested_capability": ""}')
            client.complete_with_tool("prompt A", _SUMMARY_TOOL)
            assert sdk.beta.messages.create.call_count == 1
            return
        else:
            client.config.effort = "high"
            client.complete_with_tool("prompt A", _SUMMARY_TOOL)
        assert sdk.messages.create.call_count == 2

    def test_failures_are_not_cached(self, tmp_path):
        client, sdk = _client(tmp_path)
        sdk.messages.create.return_value = _response("", )
        sdk.messages.create.return_value.stop_reason = "refusal"
        assert client.complete_with_tool("prompt A", _SUMMARY_TOOL) is None
        sdk.messages.create.return_value = _response('{"summary": "ok", "responsibility": "r", "suggested_capability": ""}')
        assert client.complete_with_tool("prompt A", _SUMMARY_TOOL)["summary"] == "ok"
        assert sdk.messages.create.call_count == 2

    def test_no_cache_when_disabled(self, tmp_path):
        client = AIClient(AIConfig(model="claude-haiku-5-5"))
        sdk = MagicMock()
        sdk.messages.create.return_value = _response('{"summary": "s", "responsibility": "r", "suggested_capability": ""}')
        client._client = sdk
        client.complete_with_tool("p", _SUMMARY_TOOL)
        client.complete_with_tool("p", _SUMMARY_TOOL)
        assert sdk.messages.create.call_count == 2

    def test_config_switch(self, tmp_path):
        from archilens.config import load_config

        (tmp_path / ".archilens.yml").write_text("ai:\n  cache: false\n", encoding="utf-8")
        assert load_config(tmp_path).ai.cache is False
        assert ArchiLensConfig().ai.cache is True


# ---------------------------------------------------------------------------
# Outlines
# ---------------------------------------------------------------------------


class TestOutline:
    def test_declarations_only_largest_file_first(self):
        files = [
            ("app/small.py", "python", "def helper():\n    return 1\n"),
            ("app/big.py", "python",
             '"""Doc."""\nimport os\n\nclass Order:\n    def total(self) -> int:\n        secret = "body"\n'
             "        return 1\n\nasync def place(order: Order) -> None:\n    pass\n"),
        ]
        outline = module_outline(files)
        assert outline.splitlines() == [
            "# app/big.py", "class Order", "    def total(self) -> int", "async def place(order: Order) -> None",
            "# app/small.py", "def helper()",
        ]
        assert "secret" not in outline and "import os" not in outline

    @pytest.mark.parametrize(
        ("language", "source", "expected"),
        [
            ("typescript", "export class A {\n  x = 1;\n}\nexport interface B {}\nconst y = 2;\nexport type C = string;\n",
             ["export class A", "export interface B", "export type C = string;"]),
            ("java", "package a;\npublic class Owner {\n  private int x;\n  public String name(int id) {\n    return \"\";\n  }\n}\n",
             ["public class Owner", "  public String name(int id)"]),
            ("go", "package p\nfunc Run(x int) error {\n\treturn nil\n}\ntype Store interface {\n}\n",
             ["func Run(x int) error", "type Store interface"]),
        ],
    )
    def test_languages(self, language, source, expected):
        assert module_outline([("f", language, source)]).splitlines()[1:] == expected

    def test_capped(self):
        source = "\n".join(f"def function_number_{i}(argument_one, argument_two, argument_three): pass" for i in range(400))
        outline = module_outline([("a.py", "python", source)], max_chars=1000)
        assert len(outline) <= 1000
        assert outline.endswith("# ...")


# ---------------------------------------------------------------------------
# Summaries: outline in the prompt, suggestions kept separate
# ---------------------------------------------------------------------------


def _module(id_: str, capability: str | None = None) -> ArchNode:
    return ArchNode(id=id_, name=id_.split("/")[-1], node_type=NodeType.MODULE, level=DiagramLevel.MODULE,
                    capability=capability)


class TestSummaries:
    def test_outline_reaches_the_prompt(self, tmp_path):
        client, sdk = _client(tmp_path)
        snap = ArchSnapshot(project_name="t", nodes=[_module("module:app/orders")])
        generate_module_summaries(snap, client, outlines={"module:app/orders": "# app/orders/x.py\nclass Order"})
        prompt = sdk.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "class Order" in prompt

    def test_suggestion_only_for_modules_without_a_capability(self, tmp_path):
        client, _ = _client(tmp_path)
        uncovered, covered = _module("module:app/orders"), _module("module:app/pay", capability="Payments")
        generate_module_summaries(ArchSnapshot(project_name="t", nodes=[uncovered, covered]), client)
        assert uncovered.suggested_capability == "Order Management"
        assert uncovered.capability is None  # never assigned
        assert covered.capability == "Payments" and covered.suggested_capability is None

    def test_engine_passes_outlines_and_entry_point_types(self, tmp_path):
        from archilens.engine import analyze_repository

        (tmp_path / "app/orders").mkdir(parents=True)
        (tmp_path / "app/orders/routes.py").write_text("def create_order():\n    pass\n", encoding="utf-8")
        (tmp_path / "app/billing").mkdir(parents=True)
        (tmp_path / "app/billing/x.py").write_text("class Invoice:\n    pass\n", encoding="utf-8")
        config = ArchiLensConfig()
        config.analysis.entry_points = [{"pattern": "**/routes.py", "type": "event_handler"}]
        config.capabilities = [Capability(name="Billing", modules=["app/billing/**"])]
        config.ai.cache = False

        with patch("archilens.ai.AIClient.check_ready", return_value=None), \
             patch("archilens.engine.infer_process_flows", return_value=[]) as flows, \
             patch("archilens.engine.generate_module_summaries", return_value={}) as summaries, \
             patch("archilens.engine.detect_patterns", return_value=[]):
            analyze_repository(tmp_path, config, use_ai=True)

        assert flows.call_args.args[0][0]["entry_type"] == "event_handler"
        outlines = summaries.call_args.kwargs["outlines"]
        assert "def create_order()" in outlines["module:app/orders"]
        assert "class Invoice" in outlines["module:app/billing"]


class TestSurfacing:
    def test_mcp_reports_suggestion_and_uses_it_for_ownership(self, tmp_path, monkeypatch):
        pytest.importorskip("mcp.server.fastmcp", reason="requires the 'mcp' extra")
        import asyncio
        import json

        from archilens.mcp import server

        node = _module("module:app/orders")
        node.suggested_capability = "Order Management"
        snap = ArchSnapshot(project_name="t", nodes=[node])
        monkeypatch.setattr(server, "_ensure_loaded", lambda: snap)
        monkeypatch.setattr(server, "_repo_path", tmp_path)
        mcp = server.create_mcp_server()

        def call(name: str, args: dict) -> dict:
            result = asyncio.run(mcp.call_tool(name, args))
            content = result[0] if isinstance(result, tuple) else result
            return json.loads(content[0].text)

        assert call("get_module_context", {"file_path": "app/orders/x.py"})["suggested_capability"] == "Order Management"
        assert call("find_capability_owner", {"feature_description": "order management"})["match"]["module_id"] == (
            "module:app/orders"
        )


class TestOutlineNeverIncludesBodies:
    @pytest.mark.parametrize(
        ("language", "line", "expected"),
        [
            ("python", "def f(self): return SECRET", "def f(self)"),
            ("python", "async def g(a: int, b: dict = {'k': 1}) -> str: return SECRET", "async def g(a: int, b: dict = {'k': 1}) -> str"),
            ("python", "class A: SECRET = 1", "class A"),
            ("python", "class B(Base): pass  # SECRET", "class B(Base)"),
            ("python", "def h(x=g()): return SECRET", "def h(x=g())"),
            ("typescript", "export function f(a: number) { return SECRET; }", "export function f(a: number)"),
            ("java", "  public String name(int id) { return SECRET; }", "  public String name(int id)"),
            ("go", "func Run() error { return SECRET }", "func Run() error"),
        ],
    )
    def test_one_line_bodies_are_cut(self, language, line, expected):
        outline = module_outline([("f", language, line + "\n")])
        assert outline.splitlines()[1:] == [expected]
        assert "SECRET" not in outline
