"""Find the JSON inside an LLM response.

Two layers, both streaming-safe:

1. A preprocessor strips ``<think>`` blocks and markdown fences. Tags and fences
   may be split across chunk boundaries, so a short tail is carried between chunks.
   If fences are present and contain JSON-like text, only the fenced text is kept.
2. A small FSM finds the first top-level ``{...}`` / ``[...]`` span in the cleaned
   text, tracking string and escape state so braces inside strings don't count.
"""
from __future__ import annotations

from dataclasses import dataclass, field

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
FENCE = "```"
TAIL = 10  # must be >= the longest token we look for (</think> is 8 chars)


@dataclass
class PreprocessState:
    in_think: bool = False
    fence_open: bool = False
    fence_lang_captured: bool = False
    carry: str = ""
    has_fences: bool = False
    saw_think: bool = False
    prev: str = "\n"  # last raw char seen; see _fence_allowed
    json_started: bool = False  # a { or [ has been emitted
    in_str: bool = False        # inside a JSON string (tracked once JSON started or in a fence)
    str_escape: bool = False
    fence_only_content: str = ""
    all_content: str = ""


@dataclass
class JsonFsmState:
    state: str = "SEEK_START"  # SEEK_START | IN_JSON | DONE | FAILED
    depth: int = 0
    in_string: bool = False
    escape: bool = False
    started_with: str = ""
    buf: list[str] = field(default_factory=list)
    max_chars: int = 5_000_000


def _fence_allowed(prev: str) -> bool:
    # Backticks right after a quote, backslash or word char are content, not a
    # fence: '{"key": "```json"' must survive intact.
    return not (prev.isalnum() or prev in "\"'\\`")


def _scan(buf: str, limit: int, state: PreprocessState) -> tuple[str, int]:
    """Consume buf[:limit], updating state. Returns (emitted text, index reached)."""
    out = []
    i = 0
    while i < limit:
        # Inside a JSON string, "<think>" and "```" are just text.
        if not state.in_str and buf.startswith(THINK_OPEN, i):
            state.in_think = True
            state.saw_think = True
            state.prev = "\n"
            i += len(THINK_OPEN)
            continue
        if state.in_think and buf.startswith(THINK_CLOSE, i):
            state.in_think = False
            state.prev = "\n"
            i += len(THINK_CLOSE)
            continue
        if (not state.in_think and not state.in_str and buf.startswith(FENCE, i)
                and _fence_allowed(state.prev)):
            # a run of 3+ backticks is a single fence
            j = i
            while j < limit and buf[j] == "`":
                j += 1
            if j == limit and limit < len(buf):
                break  # the run may continue in the carried tail; decide next time
            state.has_fences = True
            state.fence_open = not state.fence_open
            state.fence_lang_captured = not state.fence_open
            state.in_str = state.str_escape = False
            state.prev = "`"
            i = j
            continue

        c = buf[i]
        if state.fence_open and not state.fence_lang_captured and not state.in_think:
            # skip the language tag (one word) right after an opening fence
            if c.isalnum() or c in "_+-.":
                state.prev = c
                i += 1
                continue
            state.fence_lang_captured = True
        i += 1
        state.prev = c
        if state.in_think:
            continue
        if c == "\n" and not state.fence_open:
            # Outside a fence a newline ends any "string" (a stray inch mark in prose
            # must not hide the fence on the next line).
            state.in_str = state.str_escape = False
        if c in "{[":
            state.json_started = True
        if state.json_started or state.fence_open:
            if state.str_escape:
                state.str_escape = False
            elif state.in_str and c == "\\":
                state.str_escape = True
            elif c == '"':
                state.in_str = not state.in_str
        if state.fence_open:
            state.fence_only_content += c
        state.all_content += c
        out.append(c)
    return "".join(out), i


def preprocess_chunk(text: str, state: PreprocessState) -> str:
    """Feed one chunk; returns the cleaned text that is safe to emit so far."""
    buf = state.carry + (text or "")
    if len(buf) <= TAIL:
        state.carry = buf
        return ""
    out, i = _scan(buf, len(buf) - TAIL, state)
    state.carry = buf[i:]
    return out


def preprocess_finalize(state: PreprocessState) -> str:
    """Flush the carried tail at end of stream; returns the cleaned tail."""
    buf, state.carry = state.carry, ""
    out, _ = _scan(buf, len(buf), state)
    return out


def preprocess_result(state: PreprocessState) -> str:
    """Final cleaned text: fenced content if it looks like JSON, else everything."""
    fenced = state.fence_only_content
    if state.has_fences and fenced.strip() and any(c in fenced for c in "{["):
        return fenced
    return state.all_content


def preprocess(text: str) -> str:
    """One-shot preprocessing of a complete response."""
    state = PreprocessState()
    preprocess_chunk(text, state)
    preprocess_finalize(state)
    return preprocess_result(state)




def fsm_feed(state: JsonFsmState, text: str) -> None:
    if state.state in ("DONE", "FAILED"):
        return
    for ch in text:
        if state.state == "SEEK_START":
            if ch in "{[":
                state.state = "IN_JSON"
                state.started_with = ch
                state.depth = 1
                state.buf.append(ch)
            continue

        state.buf.append(ch)
        if state.in_string:
            if state.escape:
                state.escape = False
            elif ch == "\\":
                state.escape = True
            elif ch == '"':
                state.in_string = False
            continue
        if ch == '"':
            state.in_string = True
        elif ch in "{[":
            state.depth += 1
        elif ch in "}]":
            state.depth -= 1
            if state.depth == 0:
                state.state = "DONE"
                return
        if len(state.buf) >= state.max_chars:
            state.state = "FAILED"
            return


def fsm_result(state: JsonFsmState) -> tuple[str, str]:
    """Returns (json_text, status) with status DONE | TRUNCATED | FAILED."""
    if state.state == "DONE":
        return "".join(state.buf), "DONE"
    if state.state == "IN_JSON":
        return "".join(state.buf), "TRUNCATED"
    return "", "FAILED"


@dataclass
class Span:
    start: int
    end: int
    text: str
    status: str      # DONE | TRUNCATED
    in_string: bool  # truncated inside a string literal


def json_spans(text: str, limit: int = 50) -> list[Span]:
    """Every top-level {...} / [...] span in `text`, in order. Only the last can be TRUNCATED."""
    spans: list[Span] = []
    pos = 0
    while len(spans) < limit:
        starts = [i for i in (text.find("{", pos), text.find("[", pos)) if i >= 0]
        if not starts:
            break
        i = min(starts)
        fsm = JsonFsmState()
        fsm_feed(fsm, text[i:])
        span_text, status = fsm_result(fsm)
        spans.append(Span(i, i + len(span_text), span_text, status, fsm.in_string))
        if status != "DONE":
            break
        pos = i + len(span_text)
    return spans


@dataclass
class Extraction:
    cleaned: str          # response with think blocks / fences removed
    spans: list[Span]     # top-level JSON spans found in `cleaned`
    think_strip: bool
    fence_strip: bool

    @property
    def prose_extract(self) -> bool:
        return bool(self.spans) and bool(self.cleaned[: self.spans[0].start].strip())


def extract(content: str) -> Extraction:
    """Clean a complete response and locate the JSON spans in it."""
    content = content or ""
    headless_think = False
    if THINK_OPEN not in content:
        # Some models (e.g. Kimi) send reasoning with only the closing tag; anything
        # before it, including draft JSON, is reasoning. A "</think>" quoted inside a
        # JSON string, or with no JSON after it, is left alone.
        cut = content.rfind(THINK_CLOSE)
        after = content[cut + len(THINK_CLOSE):] if cut >= 0 else ""
        quoted = cut > 0 and content[cut - 1] in "\"'" or after[:1] in ("\"", "'")
        if cut >= 0 and not quoted and any(b in after for b in "{["):
            content, headless_think = after, True
    state = PreprocessState()
    preprocess_chunk(content, state)
    preprocess_finalize(state)
    cleaned = preprocess_result(state)
    spans = json_spans(cleaned)

    # An unclosed <think> swallows everything after it; look inside it as a fallback.
    if not spans and THINK_OPEN in content and THINK_CLOSE not in content:
        cleaned = content[content.find(THINK_OPEN) + len(THINK_OPEN):]
        spans = json_spans(cleaned)

    return Extraction(cleaned=cleaned, spans=spans,
                      think_strip=state.saw_think or headless_think, fence_strip=state.has_fences)
