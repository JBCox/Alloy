"""Structured output handling (spec R-5, R-24): native JSON when the provider supports a schema,
extraction from text otherwise, validation against Alloy's schemas, and exactly one bounded repair round.
A failed parse or repair never yields a value; callers cannot mistake it for approval.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..schemas import validate_against

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
MAX_REPAIR_ROUNDS = 1


@dataclass
class ParseOutcome:
    ok: bool
    value: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)
    source: str = "none"   # native | extracted | none


def _balanced_candidates(text: str):
    """Yield substrings that start at a '{' and end at the matching '}' (string-aware)."""
    n = len(text)
    for start in range(n):
        if text[start] != "{":
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, n):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


def extract_json(text: str | None) -> tuple[dict[str, Any] | None, str]:
    """Find a JSON object in free text. Returns (object, error)."""
    if not text or not text.strip():
        return None, "empty output"
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj, ""
    except ValueError:
        pass
    for m in _FENCE.finditer(text):
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj, ""
        except ValueError:
            continue
    for candidate in _balanced_candidates(text):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj, ""
        except ValueError:
            continue
    return None, "no JSON object found in output"


def parse_structured(raw_text: str | None, native: dict[str, Any] | None, schema: dict[str, Any]) -> ParseOutcome:
    if isinstance(native, dict):
        errors = validate_against(schema, native)
        return ParseOutcome(ok=not errors, value=native if not errors else None, errors=errors, source="native")
    obj, err = extract_json(raw_text)
    if obj is None:
        return ParseOutcome(ok=False, value=None, errors=[err], source="none")
    errors = validate_against(schema, obj)
    return ParseOutcome(ok=not errors, value=obj if not errors else None, errors=errors, source="extracted")


def repair_prompt(errors: list[str], previous_output: str | None, schema_name: str) -> str:
    shown = (previous_output or "").strip()
    if len(shown) > 4000:
        shown = shown[:4000] + "\n...[truncated]"
    return (
        f"Your previous reply did not match the required `{schema_name}` schema (schema.json in the packet).\n"
        "Validation errors:\n- " + "\n- ".join(errors) +
        "\n\nReply again with a single JSON object that satisfies the schema. No prose before or after the JSON.\n"
        "Do not change your findings or decisions to fit the schema; fix the structure only.\n\n"
        f"Previous reply:\n{shown}"
    )


def run_with_repair(invoke: Callable[[str | None], tuple[dict[str, Any] | None, str | None]], schema: dict[str, Any],
                    schema_name: str, max_repairs: int = MAX_REPAIR_ROUNDS) -> tuple[ParseOutcome, int]:
    """``invoke(repair_prompt_or_None)`` returns ``(native_dict_or_None, raw_text)``. At most one repair round."""
    native, raw = invoke(None)
    outcome = parse_structured(raw, native, schema)
    rounds = 0
    while not outcome.ok and rounds < max_repairs:
        rounds += 1
        native, raw = invoke(repair_prompt(outcome.errors, raw if raw else json.dumps(native) if native else "", schema_name))
        outcome = parse_structured(raw, native, schema)
    if not outcome.ok:
        outcome.value = None
    return outcome, rounds
