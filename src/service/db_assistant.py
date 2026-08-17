"""DB assistant — translate a natural-language DB request into ONE CockroachDB
MCP tool call, so the operator can review it before it runs.

felix's LLM is text-in/text-out (no native tool-calling), so this mirrors the
diagnoser's pattern: build a prompt that asks for a strict JSON object, run the
model, then parse defensively. The result is a *plan* — {tool, args, explanation}
— never executed here. The API executes it only after the operator confirms
(preview-then-confirm), via `cockroach_mcp.run_tool`.

The tool set is the additive-write allowlist in `cockroach_mcp.NL_TOOLS`
(create_table / create_database / insert_rows) plus read-only select_query; the
server exposes no drop/truncate/update/delete, so a plan can only ever create or
insert.
"""

from __future__ import annotations

import json
import re

from ..clients.cockroach_mcp import NL_TOOL_CATALOG, NL_TOOLS
from ..clients.llm import LLMClient

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_SYSTEM = (
    "You translate a single natural-language database request into exactly one "
    "CockroachDB tool call, for a human to review before it runs. You never "
    "execute anything. Only additive operations are possible (create/insert); "
    "there is no drop, delete, truncate, or update tool."
)


def _tool_menu() -> str:
    lines = []
    for t in NL_TOOL_CATALOG:
        kind = "WRITE" if t["write"] else "read-only"
        lines.append(f'- {t["name"]} ({kind}): arg "{t["arg"]}" = {t["hint"]}')
    return "\n".join(lines)


def _build_prompt(instruction: str) -> str:
    return (
        "Available tools:\n"
        f"{_tool_menu()}\n\n"
        "Choose the ONE tool that accomplishes the request and produce its argument. "
        "For CockroachDB SQL, write valid, complete statements (e.g. a CREATE TABLE with "
        "column definitions). Prefer sensible column types (UUID/STRING/INT/DECIMAL/TIMESTAMPTZ) "
        "and a primary key when creating a table. Use CREATE TABLE IF NOT EXISTS so a retry is "
        "safe. IMPORTANT: fully-qualify every table name as `defaultdb.public.<name>` unless the "
        "user named a different database — the connection's current database is `system`, which "
        "cannot be written to.\n\n"
        "Return ONLY a JSON object (no prose, no markdown fences), exactly one of:\n"
        '  {"tool": "<tool name>", "args": {"<arg>": "<value>"}, "explanation": "<one short sentence>"}\n'
        '  {"tool": null, "reason": "<why you cannot map this safely>"}\n\n'
        "Use null if the request is ambiguous, needs a destructive/unsupported operation, "
        "or isn't a database operation.\n\n"
        f"Request: {instruction!r}\n"
    )


def _extract_json_object(text: str) -> dict | None:
    """Best-effort JSON extraction: prefer a fenced block, else the outermost
    {...} span. Returns None (never raises) so the caller can fall back."""
    for candidate in reversed(_FENCED_JSON_RE.findall(text)):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def plan_operation(llm: LLMClient, instruction: str) -> dict:
    """Return a reviewable plan for `instruction`:
      {"tool", "args", "explanation", "write"}  — a valid, allowlisted tool call
      {"tool": None, "reason"}                  — couldn't map it safely

    Validates the model's choice against NL_TOOLS and coerces the arg into the
    single key that tool expects, so the plan is always executable as-is."""
    result = llm.complete(_build_prompt(instruction), system=_SYSTEM)
    obj = _extract_json_object(result.text)
    if not obj:
        return {"tool": None, "reason": "Could not understand the request."}

    tool = obj.get("tool")
    if tool is None:
        return {"tool": None, "reason": obj.get("reason") or "Request could not be mapped to a tool."}
    if tool not in NL_TOOLS:
        return {"tool": None, "reason": f"Proposed tool {tool!r} is not permitted."}

    spec = NL_TOOLS[tool]
    arg_key = spec["arg"]
    raw_args = obj.get("args") or {}
    # Normalize to exactly the one key this tool expects — tolerate the model
    # putting the value under a different key, or passing a bare string.
    if isinstance(raw_args, str):
        value = raw_args
    elif arg_key in raw_args:
        value = raw_args[arg_key]
    else:
        value = next(iter(raw_args.values()), None) if raw_args else None
    if not value or not str(value).strip():
        return {"tool": None, "reason": "The model did not produce a usable statement for that request."}

    return {
        "tool": tool,
        "args": {arg_key: str(value).strip()},
        "explanation": obj.get("explanation") or "",
        "write": bool(spec["write"]),
    }
