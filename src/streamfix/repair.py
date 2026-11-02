"""Turn an LLM response into a JSON value.

Pipeline: strip reasoning blocks and fences (``extract``), complete a truncated
``tru``/``fals``/``nul`` or a dangling ``"key":``, then hand syntax repair to
`json-repair <https://github.com/mangiucugna/json_repair>`_, which is a tolerant
parser and far broader than a set of regex fix-ups.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

import json_repair

from .extract import Extraction, extract

_PARTIAL_LITERAL = re.compile(r"([:\[,]\s*)(tr|tru|fa|fal|fals|nu|nul)$")
_DANGLING_COLON = re.compile(r":\s*$")
_TRAILING_COMMA = re.compile(r",\s*[}\]]")
_PY_LITERAL = re.compile(r"(?<![\"\w])(True|False|None)(?![\"\w])")
_JSONISH_START = tuple('{["(-0123456789')
# A curly quote is acting as a JSON delimiter when it sits next to structure:
# after { [ , : (opening) or before , : } ] (closing).
_CURLY_OPEN = re.compile(r"([{\[,:]\s*)[“”]")
_CURLY_CLOSE = re.compile(r"[“”](\s*[,:}\]])")
_CURLY_KEY = re.compile(r"[“”\"]\s*:")


@dataclass
class RepairResult:
    value: Any = None
    text: str | None = None
    status: str = "failed"  # pass | repaired | failed
    repairs: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != "failed"


def _complete_partial_literal(text: str) -> tuple[str, bool]:
    stripped = text.rstrip()
    m = _PARTIAL_LITERAL.search(stripped)
    if m:
        word = {"t": "true", "f": "false", "n": "null"}[m.group(2)[0]]
        return stripped[: m.start(2)] + word, True
    if _DANGLING_COLON.search(stripped):
        return stripped + " null", True
    return text, False


def _straighten_curly_delimiters(text: str) -> tuple[str, bool]:
    """Replace curly quotes used as JSON delimiters; leave ones inside prose alone."""
    if "“" not in text and "”" not in text:
        return text, False
    if not any(m.group(0)[0] in "“”" for m in _CURLY_KEY.finditer(text)):
        return text, False  # no curly-quoted key: probably just typography in a string
    fixed = _CURLY_CLOSE.sub(r'"\1', _CURLY_OPEN.sub(r'\1"', text))
    return fixed, fixed != text


def _classify(source: str, log: list[str], truncated: bool, known: list[str]) -> list[str]:
    names = []
    if truncated:
        names.append("close_truncated")
    if _TRAILING_COMMA.search(source):
        names.append("remove_trailing_comma")
    if _PY_LITERAL.search(source):
        names.append("fix_python_literals")
    for entry in log:
        e = entry.lower()
        if "comment" in e:
            names.append("strip_comments")
        elif "literal instead of a quote" in e:
            names.append("quote_unquoted")
    if not names and (log or not known):
        names.append("syntax_repair")
    return names


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _loads(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant)


def _finite(value: Any) -> tuple[Any, bool]:
    """Replace NaN/Infinity (not valid JSON) with None. Returns (value, changed)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None, True
    if isinstance(value, dict):
        pairs = [(k, _finite(v)) for k, v in value.items()]
        return {k: v for k, (v, _) in pairs}, any(c for _, (_, c) in pairs)
    if isinstance(value, list):
        items = [_finite(v) for v in value]
        return [v for v, _ in items], any(c for _, c in items)
    return value, False


def _finish(value: Any, repairs: list[str], log: list[str]) -> RepairResult:
    value, changed = _finite(value)
    if changed:
        repairs = [*repairs, "nan_to_null"]
    repairs = list(dict.fromkeys(repairs))
    return RepairResult(value=value, text=json.dumps(value, ensure_ascii=False),
                        status="repaired" if repairs else "pass", repairs=repairs, log=log)


def _valid(text: str) -> bool:
    try:
        _loads(text)
        return True
    except (ValueError, RecursionError):
        return False


def _choose_target(ex: Extraction) -> tuple[str, bool, bool]:
    """Pick what to repair: one JSON span, or the whole cleaned text.

    Returns (text, truncated, truncated_in_string). Spans separated by prose ("{...}
    Note: replace {name} with ...") mean only one of them is the answer: the opening
    one if the response starts with JSON, else the last valid one. Spans that are
    merely glued together ("{...}, {...}" or "{...}[...]") are left to json-repair.
    """
    spans, cleaned = ex.spans, ex.cleaned
    last = spans[-1] if spans else None
    whole = (cleaned, bool(last) and last.status == "TRUNCATED", bool(last) and last.in_string)
    if not spans:
        return whole

    def prose(gap: str) -> bool:
        return any(c.isalpha() for c in gap)

    if len(spans) == 1:
        tail = cleaned[last.end:].lstrip()
        if tail and tail[0] in ',{["':
            return whole
        chosen = last
    else:
        gaps = [cleaned[a.end:b.start] for a, b in zip(spans, spans[1:])]
        if not all(prose(g) for g in gaps):
            return whole

        def jsonish(s) -> bool:  # rules out prose brackets like "[0, 10)" or ":-["
            return '"' in s.text or (s.text.startswith("{") and ":" in s.text)

        first, valid = spans[0], [s for s in spans if _valid(s.text)]
        if not cleaned[: first.start].strip() and (first in valid or jsonish(first)):
            chosen = first  # the response opens with JSON: that's the answer, then notes
        elif last.status == "TRUNCATED" and jsonish(last):
            chosen = last  # the answer still being written when the output stopped
        elif valid:
            chosen = valid[-1]  # corrections, results and final answers come last
        else:
            chosen = max(spans, key=lambda s: len(s.text))
    return chosen.text, chosen.status == "TRUNCATED", chosen.in_string


def repair(content: str) -> RepairResult:
    """Extract and repair the JSON in `content`. Never raises on bad input."""
    try:
        return _repair(content)
    except RecursionError:  # nesting deeper than Python can walk
        return RepairResult()


def _repair(content: str) -> RepairResult:
    if not isinstance(content, str) or not content.strip():
        return RepairResult()

    repairs: list[str] = []
    if content.startswith("﻿"):
        content = content[1:]
        repairs.append("bom_strip")

    try:
        return _finish(_loads(content), repairs, [])
    except (ValueError, RecursionError):
        pass

    ex = extract(content)
    if ex.think_strip:
        repairs.append("think_strip")
    if ex.fence_strip:
        repairs.append("fence_strip")
    if ex.prose_extract:
        repairs.append("prose_extract")

    cleaned, truncated, in_string = _choose_target(ex)
    if truncated and not in_string:
        cleaned, done = _complete_partial_literal(cleaned)
        if done:
            repairs.append("complete_partial_literal")

    cleaned, straightened = _straighten_curly_delimiters(cleaned)
    if straightened:
        repairs.append("fix_curly_quotes")

    try:
        return _finish(_loads(cleaned), repairs, [])
    except (ValueError, RecursionError):
        pass

    try:
        value, raw_log = json_repair.repair_json(cleaned, return_objects=True, logging=True)
    except Exception:  # json-repair raises ValueError/RecursionError on pathological nesting
        return RepairResult(repairs=repairs)
    log = [entry.get("text", "") for entry in raw_log]
    # json-repair returns "" when it finds nothing. A bare scalar pulled out of prose
    # is noise, so only accept scalars when the text itself starts like JSON.
    if value == "" or (
        not isinstance(value, (dict, list))
        and not cleaned.lstrip().startswith(_JSONISH_START)
    ):
        return RepairResult(repairs=repairs, log=log)
    repairs += _classify(cleaned, log, truncated, repairs)
    return _finish(value, repairs, log)


def repair_tool_arguments(arguments: str) -> tuple[str, bool]:
    """Repair `tool_calls[].function.arguments`. Returns (arguments, was_repaired)."""
    if not arguments:
        return arguments, False
    if not isinstance(arguments, str):
        return arguments, False
    if _valid(arguments):
        return arguments, False
    result = repair(arguments)
    if result.ok and result.text is not None:
        return result.text, True
    return arguments, False
