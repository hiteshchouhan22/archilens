"""
AI usage reporting: the client totals requests, cache hits and tokens, and the
diff report and analyze log show them.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from archilens.ai import AIClient
from archilens.ai.cache import AICache
from archilens.config import AIConfig
from archilens.generators.diff_report import format_diff_as_markdown
from archilens.models import AIUsage, ArchDiff, DiffReview

TOOL = {"name": "record", "input_schema": {"type": "object", "properties": {}, "required": []}}


def _anthropic_response(**usage: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(**usage),
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="{}")],
    )


def _client(response: SimpleNamespace, cache: AICache | None = None, model: str = "claude-opus-5-5") -> AIClient:
    client = AIClient(AIConfig(model=model), cache=cache)
    fake = MagicMock()
    fake.messages.create.return_value = response
    fake.beta.messages.create.return_value = response
    client._client = fake
    return client


class TestUsageText:
    def test_plural_and_thousands(self):
        usage = AIUsage(requests=2, input_tokens=8912, output_tokens=1204, cached=1)
        assert usage.text() == "2 requests: 8,912 input and 1,204 output tokens (1 more from cache)"

    def test_single_request_without_cache_hits(self):
        assert AIUsage(requests=1, input_tokens=5, output_tokens=7).text() == "1 request: 5 input and 7 output tokens"

    def test_everything_from_cache(self):
        assert AIUsage(cached=3).text() == "answered from cache (3 requests, no tokens used)"


class TestClientCounts:
    def test_anthropic_input_includes_prompt_cache_tokens(self):
        client = _client(_anthropic_response(input_tokens=100, cache_creation_input_tokens=20,
                                             cache_read_input_tokens=30, output_tokens=40))
        assert client.complete_with_tool("p", TOOL) == {}
        assert client.usage == AIUsage(requests=1, input_tokens=150, output_tokens=40)

    def test_forced_tool_path_is_counted_too(self):
        response = _anthropic_response(input_tokens=10, output_tokens=5)
        response.content = [SimpleNamespace(type="tool_use", input={"x": 1})]
        client = _client(response, model="claude-sonnet-4-5")
        assert client.complete_with_tool("p", TOOL) == {"x": 1}
        assert client.usage == AIUsage(requests=1, input_tokens=10, output_tokens=5)

    def test_litellm_usage_fields(self):
        client = AIClient(AIConfig(provider="litellm", model="gpt-x"))
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
        )
        client._client = MagicMock(completion=MagicMock(return_value=response))
        client.complete_with_tool("p", TOOL)
        assert client.usage == AIUsage(requests=1, input_tokens=11, output_tokens=3)

    def test_missing_usage_still_counts_the_request(self):
        client = _client(SimpleNamespace(usage=None, stop_reason="end_turn",
                                         content=[SimpleNamespace(type="text", text="{}")]))
        client.complete_with_tool("p", TOOL)
        assert client.usage == AIUsage(requests=1)

    def test_cache_hits_cost_nothing(self, tmp_path: Path):
        cache = AICache(tmp_path / "cache.db")
        client = _client(_anthropic_response(input_tokens=10, output_tokens=5), cache=cache)
        client.complete_with_tool("p", TOOL)
        client.complete_with_tool("p", TOOL)
        assert client.usage == AIUsage(requests=1, cached=1, input_tokens=10, output_tokens=5)

    def test_parallel_requests_are_all_counted(self):
        client = _client(_anthropic_response(input_tokens=1, output_tokens=1))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: client.complete_with_tool(f"p{i}", TOOL), range(200)))
        assert client.usage == AIUsage(requests=200, input_tokens=200, output_tokens=200)


def test_report_credits_the_usage():
    diff = ArchDiff(base_ref="a", head_ref="b")
    diff.review = DiffReview(model="claude-opus-5-5", summary="s",
                             usage=AIUsage(requests=1, input_tokens=4200, output_tokens=310))
    assert (
        "<sub>Written by claude-opus-5-5 from the changes below, which are the source of truth. "
        "AI usage: 1 request: 4,200 input and 310 output tokens.</sub>"
    ) in format_diff_as_markdown(diff)
