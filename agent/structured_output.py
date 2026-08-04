"""Provider-wire mapping for API-server structured-output constraints.

Adapted from the normalization and wire-mapping work in NousResearch/hermes-agent
PR #39595 by Miguel Fernandez.  Boundary validation and persistence remain owned
by the API server; this leaf module only describes transport support and shape.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


SUPPORTED_API_MODES = frozenset({
    "chat_completions",
    "codex_responses",
    "anthropic_messages",
})


def unsupported_reason(
    constraint: Optional[Dict[str, Any]], api_mode: Optional[str]
) -> Optional[str]:
    if not constraint:
        return None
    if api_mode not in SUPPORTED_API_MODES:
        return f"Structured output is not supported for api_mode={api_mode!r}."
    if api_mode == "anthropic_messages" and constraint.get("type") == "json_object":
        return "Anthropic Messages structured output requires a json_schema."
    return None


def apply_anthropic_format(
    kwargs: Dict[str, Any], constraint: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    if not constraint or constraint.get("type") != "json_schema":
        return kwargs
    schema = constraint.get("json_schema", {}).get("schema")
    if not isinstance(schema, dict):
        return kwargs
    output_config = dict(kwargs.get("output_config") or {})
    output_config.setdefault("format", {"type": "json_schema", "schema": schema})
    kwargs["output_config"] = output_config
    return kwargs
