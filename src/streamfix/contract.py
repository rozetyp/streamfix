"""Contract Mode: make a repaired value fit a JSON Schema.

`coerce_types` fixes the type drift models commonly produce ("30" for 30,
"true" for true, 30.0 for 30) where the schema says what the type should be.
`validate` reports what still doesn't fit, and `retry_prompt` turns that into a
correction message for one more model attempt.
"""
from __future__ import annotations

import math
from typing import Any, Iterator

import jsonschema
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable


def _no_remote(uri: str):
    raise NoSuchResource(ref=uri)


# $refs resolve inside the schema only; nothing is ever fetched over the network.
_REGISTRY = Registry(retrieve=_no_remote)


def _nodes(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _nodes(v)
    elif isinstance(node, list):
        for v in node:
            yield from _nodes(v)


def check_schema(schema: Any, allow_patterns: bool = True) -> str | None:
    """Return an error message if `schema` is not usable, else None.

    Only local `$ref`s ("#/...") are accepted. With ``allow_patterns=False`` (the
    proxy's setting) regex keywords are refused too, because a caller-supplied
    regex can take exponential time.
    """
    if not isinstance(schema, (dict, bool)):
        return "schema must be a JSON object"
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        return e.message
    for node in _nodes(schema):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            return f"only local $refs are allowed, got {ref!r}"
        if not allow_patterns and ("pattern" in node or "patternProperties" in node):
            return "regex keywords (pattern, patternProperties) are not accepted here"
    return None


def validate(value: Any, schema: Any) -> list[str]:
    """Return human-readable violations (empty if `value` fits `schema`)."""
    errors = []
    try:
        for e in jsonschema.Draft7Validator(schema, registry=_REGISTRY).iter_errors(value):
            path = ".".join(str(p) for p in e.absolute_path) or "root"
            errors.append(f"at {path}: {e.message}")
    except (Unresolvable, jsonschema.exceptions.SchemaError) as e:
        errors.append(f"schema could not be used: {e}")
    return errors


def _coerce_scalar(v: Any, t: str) -> tuple[Any, bool]:
    """Coerce one value to JSON-Schema type `t` if that is safe. Returns (value, changed)."""
    if t in ("integer", "number") and isinstance(v, str):
        try:
            return int(v.strip()), True  # exact, even beyond float precision
        except ValueError:
            pass
        try:
            f = float(v.strip())
        except ValueError:
            return v, False
        if not math.isfinite(f):
            return v, False
        if t == "integer":
            return (int(f), True) if f.is_integer() else (v, False)
        return (int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f), True
    if t == "integer" and isinstance(v, float) and v.is_integer():
        return int(v), True
    if t == "boolean" and isinstance(v, str) and v.strip().lower() in _BOOLS:
        return _BOOLS[v.strip().lower()], True
    return v, False


def _fits(v: Any, t: str) -> bool:
    return {
        "string": isinstance(v, str),
        "integer": isinstance(v, int) and not isinstance(v, bool),
        "number": isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": isinstance(v, bool),
        "null": v is None,
        "object": isinstance(v, dict),
        "array": isinstance(v, list),
    }.get(t, False)


_BOOLS = {"true": True, "false": False, "yes": True, "no": False, "1": True, "0": False}


def coerce_types(value: Any, schema: dict) -> tuple[Any, list[str]]:
    """Coerce values toward the schema's declared types. Returns (value, coercions)."""
    coercions: list[str] = []

    def walk(v: Any, s: Any, path: str) -> Any:
        if not isinstance(s, dict):
            return v
        types = s.get("type")
        types = types if isinstance(types, list) else [types] if types else []
        if v is None and "null" in types:
            return v
        if isinstance(v, dict) and ("object" in types or "properties" in s):
            props = s.get("properties") or {}
            extra = s.get("additionalProperties")
            return {k: walk(x, props.get(k, extra), f"{path}.{k}") for k, x in v.items()}
        if isinstance(v, list) and ("array" in types or "items" in s):
            return [walk(x, s.get("items"), f"{path}[{i}]") for i, x in enumerate(v)]
        if any(_fits(v, t) for t in types):
            return v  # already valid: "02134" stays a string when strings are allowed
        for t in types:
            if t == "null":
                continue
            new, changed = _coerce_scalar(v, t)
            if changed:
                coercions.append(f"{path}: {v!r} -> {new!r}")
                return new
        return v

    return walk(value, schema, "root"), coercions


def retry_prompt(errors: list[str], schema: dict) -> str:
    """Correction message for a second attempt after a schema violation."""
    lines = [
        "Your previous JSON did not match the required schema.",
        "Problems: " + "; ".join(errors[:5]),
    ]
    required = schema.get("required") if isinstance(schema, dict) else None
    if required:
        lines.append("Required fields: " + ", ".join(map(str, required)))
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and props:
        parts = []
        for name, p in props.items():
            p = p if isinstance(p, dict) else {}
            desc = f"{name}: {p.get('type', 'any')}"
            if "enum" in p:
                desc += " (one of: " + ", ".join(repr(x) for x in p["enum"]) + ")"
            parts.append(desc)
        lines.append("Field types: " + ", ".join(parts))
    lines.append("Return ONLY the corrected JSON. No explanation, no markdown fences.")
    return "\n".join(lines)
