"""
AI-powered analysis using LLMs for capabilities beyond static analysis.

Handles:
- Process flow inference from controller/handler code
- Natural-language annotations for diagram nodes
- Business capability mapping suggestions
- Architectural pattern detection
- Module responsibility summaries
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, cast

from archilens.config import AIConfig
from archilens.models import (
    ArchNode,
    ArchPattern,
    ArchSnapshot,
    FlowStep,
    ProcessFlow,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------
# Each schema is sent either as a structured-output JSON schema (current Claude
# models) or as a forced tool's input schema (older models). Structured outputs
# need every object closed and every field required, so optional values are
# returned as empty strings / false instead of being left out.

_PATTERN_NAMES = [p.value for p in ArchPattern]

_FLOW_TOOL: dict[str, Any] = {
    "name": "record_flow",
    "description": "Record the extracted process flow with all its steps.",
    "input_schema": {
        "type": "object",
        "properties": {
            "flow_name": {"type": "string", "description": "Descriptive name, e.g. 'Create Order Flow'"},
            "trigger": {"type": "string", "description": "What initiates this flow, e.g. 'POST /api/orders'"},
            "description": {"type": "string", "description": "One-sentence summary"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "order": {"type": "integer"},
                        "actor": {"type": "string"},
                        "action": {"type": "string"},
                        "target": {"type": "string"},
                        "data": {"type": "string", "description": "Data exchanged, or empty string"},
                        "is_async": {"type": "boolean"},
                        "condition": {"type": "string", "description": "Guard condition, or empty string"},
                    },
                    "required": ["order", "actor", "action", "target", "data", "is_async", "condition"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["flow_name", "trigger", "description", "steps"],
        "additionalProperties": False,
    },
}

_SUMMARY_TOOL: dict[str, Any] = {
    "name": "record_summary",
    "description": "Record a concise module summary.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "One sentence describing what this module does"},
            "responsibility": {"type": "string", "description": "Primary responsibility in 3-5 words"},
            "suggested_capability": {
                "type": "string",
                "description": "Business capability this maps to (e.g. 'Order Management', 'Authentication')",
            },
        },
        "required": ["summary", "responsibility", "suggested_capability"],
        "additionalProperties": False,
    },
}

_PATTERN_TOOL: dict[str, Any] = {
    "name": "record_patterns",
    "description": "Record detected architectural patterns.",
    "input_schema": {
        "type": "object",
        "properties": {
            "patterns": {
                "type": "array",
                "description": "Detected patterns, each with a brief reason",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": _PATTERN_NAMES},
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["patterns"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PROCESS_FLOW_PROMPT = """You are an expert software architect analyzing source code to extract runtime process flows.

Given the following source code from an entry point (controller/handler/route), identify the end-to-end request lifecycle.

**File:** {file_path}
**Entry Point Type:** {entry_type}

```{language}
{code}
```

Additional context - files that this entry point imports or calls:
{dependency_context}

Analyze this code and record the complete process flow."""

MODULE_SUMMARY_PROMPT = """You are an expert software architect. Given the following module information, provide a concise summary.

**Module:** {module_name}
**Files:**
{file_list}

**Key classes/functions found:**
{symbols}

**Dependencies (imports from other modules):**
{dependencies}

Record a one-sentence summary of this module's responsibility."""

PATTERN_DETECTION_PROMPT = """You are an expert software architect. Given the following architecture overview, identify which architectural patterns are present.

**Project:** {project_name}
**Modules and their relationships:**
{module_graph}

**Directory structure:**
{directory_structure}

Record every architectural pattern that is present, with a brief reason for each."""


# ---------------------------------------------------------------------------
# AI Client abstraction
# ---------------------------------------------------------------------------


class AIClient:
    """
    Abstraction over LLM providers.

    Supports Anthropic (Claude), OpenAI, Ollama, and any
    OpenAI-compatible endpoint via LiteLLM.

    For Anthropic, the request shape depends on the model generation:

    - Claude 5-generation models (e.g. ``claude-opus-5-5``, ``claude-haiku-5-5``):
      structured outputs (``output_config.format``) plus ``effort``. Several of
      these models reject a forced ``tool_choice`` with a 400.
    - Older models (Claude 4.x and earlier): a forced tool call, which those
      models support and structured outputs may not.
    """

    def __init__(self, config: AIConfig):
        self.config = config
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize the LLM client."""
        if self._client is not None:
            return self._client

        if self.config.provider == "anthropic":
            try:
                import anthropic

                self._client = anthropic.Anthropic()
                return self._client
            except ImportError as exc:
                raise RuntimeError("anthropic package not installed. Run: pip install anthropic") from exc
        else:
            try:
                import litellm

                self._client = litellm
                return self._client
            except ImportError as exc:
                raise RuntimeError("litellm package not installed. Run: pip install litellm") from exc

    def check_ready(self) -> str | None:
        """
        Check that AI calls can work before running them. Returns None when ready,
        otherwise a one-line reason (no credentials, unknown model, no network).

        For Anthropic this looks the model up via the Models API, which verifies
        credentials and the model ID without spending tokens.
        """
        if self.config.provider != "anthropic":
            return None
        try:
            import anthropic

            client = self._get_client()
            client.models.retrieve(self.config.model)
        except RuntimeError as exc:
            return str(exc)
        except (TypeError, anthropic.CredentialsError):
            # The SDK raises TypeError when no credential source can be resolved.
            return "no Anthropic credentials found"
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            return "the Anthropic credentials were rejected"
        except anthropic.NotFoundError:
            return f"model {self.config.model!r} was not found (check ai.model in .archilens.yml)"
        except anthropic.APIConnectionError:
            return "the Anthropic API could not be reached"
        except Exception as exc:  # unexpected: don't block analysis on the preflight itself
            logger.debug("AI preflight check failed unexpectedly: %s", exc)
        return None

    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        """Send a prompt and return the response text."""
        client = self._get_client()

        if self.config.provider == "anthropic":
            response = client.messages.create(
                model=self.config.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return _response_text(response)
        else:
            response = client.completion(
                model=self.config.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
            return cast(str, response.choices[0].message.content)

    def complete_with_tool(
        self,
        prompt: str,
        tool: dict[str, Any],
        max_tokens: int = 1024,
    ) -> dict[str, Any] | None:
        """
        Get a JSON object matching ``tool["input_schema"]``.

        Anthropic, current models: structured outputs, so the reply is guaranteed
        to be valid JSON for the schema. Anthropic, older models: a forced tool call.
        Other providers: plain text, with the schema in the prompt, parsed as JSON.

        Returns the parsed object, or None on failure or refusal.
        """
        if self.config.provider != "anthropic":
            return _parse_json_response(self.complete(_with_schema_instructions(prompt, tool), max_tokens))

        try:
            if _is_current_generation(self.config.model):
                return self._structured_output(prompt, tool)
            return self._forced_tool(prompt, tool, max_tokens)
        except Exception as exc:
            logger.warning("AI request failed (%s): %s", tool["name"], exc)
            return None

    def _structured_output(self, prompt: str, tool: dict[str, Any]) -> dict[str, Any] | None:
        """Claude 5-generation models: JSON schema output plus effort, with refusal fallback."""
        client = self._get_client()
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": tool["input_schema"]}}
        if self.config.effort:
            output_config["effort"] = self.config.effort
        request: dict[str, Any] = {
            "model": self.config.model,
            # Thinking is always on for these models and counts toward max_tokens.
            "max_tokens": _STRUCTURED_MAX_TOKENS,
            "output_config": output_config,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.config.model in _SERVER_FALLBACK_MODELS:
            # If a safety classifier declines (e.g. on security-related source code),
            # the API re-runs the request on a model picked for that refusal category.
            response = client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **request)
        else:
            response = client.messages.create(**request)

        if response.stop_reason == "refusal":
            logger.warning("Model declined the %s request; skipping it.", tool["name"])
            return None
        if response.stop_reason == "max_tokens":
            logger.warning("Model output for %s was cut off at max_tokens; skipping it.", tool["name"])
            return None
        return _parse_json_response(_response_text(response))

    def _forced_tool(self, prompt: str, tool: dict[str, Any], max_tokens: int) -> dict[str, Any] | None:
        """Claude 4.x and older: force a call to the tool to get schema-shaped input."""
        response = self._get_client().messages.create(
            model=self.config.model,
            max_tokens=max_tokens,
            tools=[
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool["input_schema"],
                }
            ],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if block.type == "tool_use":
                return cast("dict[str, Any]", block.input)
        return None


# Thinking is always on for current models and shares max_tokens with the answer;
# a low cap risks truncating the JSON. Non-streaming requests stay well below the
# SDK's HTTP timeout at this size.
_STRUCTURED_MAX_TOKENS = 16000

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_SERVER_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

# Claude 4.x and older, e.g. "claude-sonnet-4-20250514", "claude-opus-4-8", "claude-3-5-haiku-latest".
_LEGACY_MODEL = re.compile(r"^claude-(?:\d|instant|[a-z]+-[1-4](?:$|[-.@]))")


def _is_current_generation(model: str) -> bool:
    """True for Claude 5-generation models and anything newer or unrecognised."""
    return not _LEGACY_MODEL.match(model)


def _response_text(response: Any) -> str:
    """Concatenate the text blocks of a Messages API response (skips thinking/fallback blocks)."""
    return "".join(block.text for block in response.content if block.type == "text")


def _with_schema_instructions(prompt: str, tool: dict[str, Any]) -> str:
    """For providers without structured outputs: ask for bare JSON matching the schema."""
    return (
        f"{prompt}\n\nRespond with only a JSON object (no prose, no code fences) that matches "
        f"this JSON schema:\n{json.dumps(tool['input_schema'], indent=2)}"
    )


# ---------------------------------------------------------------------------
# AI Analysis Functions
# ---------------------------------------------------------------------------


def infer_process_flows(
    entry_point_files: list[dict[str, str]],
    ai_client: AIClient,
    repo_path: Path,
) -> list[ProcessFlow]:
    """
    Use AI to infer process flows from entry point source code.

    Args:
        entry_point_files: List of dicts with keys: path, language, entry_type, dependencies
        ai_client: Configured AI client
        repo_path: Repository root path

    Returns:
        List of ProcessFlow objects
    """
    flows: list[ProcessFlow] = []

    for ep in entry_point_files:
        file_path = repo_path / ep["path"]
        if not file_path.exists():
            continue

        try:
            code = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if len(code) > 15000:
            code = code[:15000] + "\n# ... (truncated)"

        prompt = PROCESS_FLOW_PROMPT.format(
            file_path=ep["path"],
            entry_type=ep.get("entry_type", "http_handler"),
            language=ep.get("language", "python"),
            code=code,
            dependency_context=ep.get("dependencies", "No additional context available."),
        )

        try:
            flow_data = ai_client.complete_with_tool(prompt, _FLOW_TOOL, max_tokens=2048)

            if flow_data:
                flow = ProcessFlow(
                    id=f"flow:{ep['path']}",
                    name=flow_data.get("flow_name", f"Flow: {ep['path']}"),
                    description=flow_data.get("description", ""),
                    trigger=flow_data.get("trigger", ""),
                    entry_point=ep["path"],
                    steps=[
                        FlowStep(
                            order=s.get("order", i),
                            actor=s.get("actor", "Unknown"),
                            action=s.get("action", ""),
                            target=s.get("target", ""),
                            data=s.get("data") or None,
                            is_async=s.get("is_async", False),
                            condition=s.get("condition") or None,
                        )
                        for i, s in enumerate(flow_data.get("steps", []))
                    ],
                )
                flows.append(flow)
                logger.info("Inferred flow: %s (%d steps)", flow.name, len(flow.steps))
        except Exception as exc:
            logger.warning("AI flow inference failed for %s: %s", ep["path"], exc)

    return flows


def generate_module_summaries(
    snapshot: ArchSnapshot,
    ai_client: AIClient,
    max_workers: int = 5,
) -> dict[str, dict[str, str]]:
    """
    Generate AI-powered summaries for each module, running up to *max_workers*
    completions in parallel via ThreadPoolExecutor.

    Returns dict: module_id -> {"summary": ..., "responsibility": ..., "suggested_capability": ...}
    """
    summaries: dict[str, dict[str, str]] = {}
    module_nodes = snapshot.get_module_nodes()

    def _summarize_one(node: ArchNode) -> tuple[str, dict[str, str] | None]:
        children = snapshot.get_children(node.id)
        symbols = ", ".join(c.name for c in children[:20])
        deps = [e.target.replace("module:", "") for e in snapshot.edges if e.source == node.id]

        prompt = MODULE_SUMMARY_PROMPT.format(
            module_name=node.name,
            file_list=node.file_path or "N/A",
            symbols=symbols or "N/A",
            dependencies=", ".join(deps) if deps else "None",
        )

        try:
            result = ai_client.complete_with_tool(prompt, _SUMMARY_TOOL, max_tokens=500)
            return node.id, result
        except Exception as exc:
            logger.warning("AI summary failed for %s: %s", node.name, exc)
            return node.id, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_summarize_one, node): node for node in module_nodes}
        for future in as_completed(futures):
            try:
                node_id, result = future.result()
                if result:
                    summaries[node_id] = result
                    # Write ai_summary back onto the node in-place
                    for node in module_nodes:
                        if node.id == node_id:
                            node.ai_summary = result.get("summary", "")
                            break
            except Exception as exc:
                logger.warning("Summary future failed: %s", exc)

    return summaries


def detect_patterns(
    snapshot: ArchSnapshot,
    directory_tree: str,
    ai_client: AIClient,
) -> list[ArchPattern]:
    """Use AI to detect architectural patterns in the codebase."""
    module_graph_lines: list[str] = []
    for node in snapshot.get_module_nodes():
        deps = [e.target.replace("module:", "") for e in snapshot.edges if e.source == node.id]
        dep_str = " -> " + ", ".join(deps) if deps else " (no dependencies)"
        module_graph_lines.append(f"  {node.name}{dep_str}")

    prompt = PATTERN_DETECTION_PROMPT.format(
        project_name=snapshot.project_name,
        module_graph="\n".join(module_graph_lines),
        directory_structure=directory_tree[:3000],
    )

    try:
        result = ai_client.complete_with_tool(prompt, _PATTERN_TOOL, max_tokens=1000)
        if result and "patterns" in result:
            names = [p.get("name") if isinstance(p, dict) else p for p in result["patterns"]]
            valid_values = {e.value for e in ArchPattern}
            return list(dict.fromkeys(ArchPattern(n) for n in names if n in valid_values))
    except Exception as exc:
        logger.warning("AI pattern detection failed: %s", exc)

    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json_response(text: str) -> dict[str, Any] | None:
    """
    Parse JSON from an LLM response for non-Anthropic providers that don't
    support tool use.  Handles markdown code fences and partial wrapping.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        return cast("dict[str, Any]", json.loads(text))
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return cast("dict[str, Any]", json.loads(text[start:end]))
            except json.JSONDecodeError:
                pass

    logger.warning("Failed to parse JSON from AI response")
    return None
