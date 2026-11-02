"""Repair JSON while it streams.

`StreamRepairer` rewrites text character by character as it arrives, so a client
rendering a stream sees valid JSON syntax without waiting for the end. It holds
back only what it must: a comma until the next token shows whether it was a
trailing comma, and a bare word (``True``, ``None``, ``active``) until it ends.

`StreamProcessor` puts the preprocessor (``<think>``/fence stripping) in front of
it and keeps the raw text, so `result()` can run the full non-streaming `repair`
for an authoritative final value.
"""
from __future__ import annotations

import json
import re

from .extract import PreprocessState, preprocess_chunk, preprocess_finalize
from .repair import RepairResult, repair

_NUMBER = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?$")
_LITERALS = {"true": "true", "false": "false", "null": "null",
             "True": "true", "False": "false", "None": "null"}
_PARTIAL = {"tr": "true", "tru": "true", "fa": "false", "fal": "false", "fals": "false",
            "nu": "null", "nul": "null"}
_CLOSER = {"{": "}", "[": "]"}


def _is_word_char(c: str) -> bool:
    """Anything that isn't JSON structure, a quote, a comment slash or whitespace.

    Stray punctuation (`x`, (1, 2), 1;) becomes part of a bare word, which is then
    quoted, so it can never leak into the output as invalid JSON.
    """
    return not c.isspace() and c not in '{}[],:"\'/'


def _is_atom(word: str) -> bool:
    """A complete number or literal: a following word is a new value, not a continuation."""
    return word in _LITERALS or bool(_NUMBER.match(word))


def _plain(c: str) -> str:
    """A character inside a JSON string, escaped if it has to be."""
    if c == "\n":
        return "\\n"
    if c == "\r":
        return "\\r"
    if c == "\t":
        return "\\t"
    if ord(c) < 0x20:
        return "\\u%04x" % ord(c)
    return c


def _render_word(word: str, final: bool = False) -> str:
    if word in _LITERALS:
        return _LITERALS[word]
    if final and word in _PARTIAL:
        return _PARTIAL[word]
    if _NUMBER.match(word):
        sign = "-" if word.startswith("-") else ""
        body = word.lstrip("-")
        int_part, _, rest = body.partition(".")
        int_part = int_part.lstrip("0") or "0"
        return sign + int_part + ("." + rest if rest else "")
    return json.dumps(word)


class StreamRepairer:
    """Character-level JSON repair for text arriving in pieces.

    Text outside the first top-level JSON value passes through untouched, or is
    dropped when ``json_only`` is set.
    """

    def __init__(self, json_only: bool = False):
        self.json_only = json_only
        self.stack: list[str] = []
        self.done = False
        self.emitted_json = False
        # inside a string
        self.in_string = False
        self.quote = '"'
        self.string_is_key = False
        self.escape = False
        self.unicode: str | None = None  # hex digits collected after \u
        # where we are in the structure
        self.expect_key = False   # next token in an object is a key
        self.key_done = False     # a quoted key closed; ':' not seen yet
        self.after_colon = False  # ':' written; value not started
        self.value_done = False   # a value just ended; another token needs a comma first
        self.empty = False        # container just opened
        # text held back until it is unambiguous
        self.key: str | None = None  # unquoted key being collected
        self.word = ""
        self.word_ws = ""  # whitespace after a bare word, held in case the word continues
        self.comment = ""  # "" | "/" | "line" | "block" | "block*"
        self.pending_comma = False
        self.pending_ws: list[str] = []

    def feed(self, text: str) -> str:
        out: list[str] = []
        for c in text:
            self._char(c, out)
        return "".join(out)

    def finish(self) -> str:
        """Flush held characters and close anything a truncated stream left open."""
        if not self.stack or self.done:
            return ""
        out: list[str] = []
        if self.in_string:
            # a dangling backslash or partial \u escape is dropped
            out.append('"')
            self.in_string = False
            if self.string_is_key:
                out.append(": null")
        elif self.key is not None:
            out.append(json.dumps(self.key.strip()) + ": null")
        elif self.word:
            out.append(_render_word(self.word, final=True))
        elif self.after_colon:
            out.append("null")
        elif self.key_done:
            out.append(": null")
        out.extend(_CLOSER[b] for b in reversed(self.stack))
        self.stack.clear()
        self.done = True
        return "".join(out)

    def _string_char(self, c: str, out: list[str]) -> None:
        if self.unicode is not None:
            if c in "0123456789abcdefABCDEF" and len(self.unicode) < 4:
                self.unicode += c
                if len(self.unicode) == 4:
                    out.append("\\u" + self.unicode)
                    self.unicode = None
                return
            out.append("\\\\u" + self.unicode)  # not a real escape: keep it as text
            self.unicode = None
        if self.escape:
            self.escape = False
            if c == "u":
                self.unicode = ""
            elif c in '"\\/bfnrt':
                out.append("\\" + c)
            elif c == "'":
                out.append("'")
            else:
                out.append("\\\\" + _plain(c))
        elif c == "\\":
            self.escape = True
        elif c == self.quote:
            self.in_string = False
            out.append('"')
            if self.string_is_key:
                self.key_done = True
            else:
                self.value_done = True
        elif c == '"':  # inside a single-quoted string
            out.append('\\"')
        else:
            out.append(_plain(c))

    def _end_word(self, out: list[str]) -> None:
        out.append(_render_word(self.word) + self.word_ws)
        self.word, self.word_ws = "", ""
        self.value_done = True

    def _char(self, c: str, out: list[str]) -> None:
        if self.done or (not self.stack and c not in "{["):
            if not self.json_only:
                out.append(c)
            return
        if self.in_string:
            self._string_char(c, out)
            return

        if self.comment:
            if self.comment == "/":
                if c in "/*":
                    self.comment = "line" if c == "/" else "block"
                    return
                # A lone slash is not JSON outside a string; keep it in a bare word.
                self.comment = ""
                self.word = (self.word + self.word_ws + "/") if self.word else "/"
                self.word_ws = ""
            elif self.comment == "line":
                if c == "\n":
                    self.comment = ""
                    self._char(c, out)
                return
            else:
                self.comment = "block*" if c == "*" else ("" if self.comment == "block*" and c == "/" else "block")
                return
        elif c == "/" and (not self.word or self.word_ws) and self.key is None:
            self.comment = "/"  # after "1 " this starts a comment; in "http:/" it doesn't
            return

        if self.key is not None:  # unquoted key: collect until ':'
            if c == ":":
                out.append(json.dumps(self.key.strip()) + ":")
                self.key = None
                self.after_colon = True
                return
            if c not in ",}]":
                self.key += c
                return
            out.append(json.dumps(self.key.strip()) + ": null")
            self.key = None
            self.value_done = True

        if self.word:
            if c.isspace():
                self.word_ws += c
                return
            continues = _is_word_char(c) or c in ":/"
            if continues and not (self.word_ws and _is_atom(self.word)):
                # "in progress", "http://x", "10:30" or a literal split by a newline
                self.word += self.word_ws + c
                self.word_ws = ""
                return
            self._end_word(out)

        if c.isspace():
            (self.pending_ws if self.pending_comma else out).append(c)
            return

        if self.pending_comma:
            if c == ",":
                return  # doubled comma
            # Held until now: a comma directly before a closer was a trailing comma.
            if c not in "}]":
                out.append(",")
                self.expect_key = self.stack[-1] == "{"
            out.extend(self.pending_ws)
            self.pending_comma, self.pending_ws = False, []
        elif c == "," or (c == ":" and self.stack[-1] == "["):  # ':' in an array acts as ','
            if self.after_colon:
                out.append("null")
            elif self.key_done:
                out.append(": null")
            elif self.empty:
                return  # leading comma, as in "{, ..."
            self.after_colon = self.key_done = self.value_done = False
            self.pending_comma = True
            return
        elif c not in "}]:":
            if self.key_done:
                out.append(":")  # missing colon after a key
                self.key_done, self.after_colon = False, True
            elif self.value_done:
                out.append(",")  # missing comma between values
                self.expect_key = self.stack[-1] == "{"
        self.value_done = False

        self.emitted_json = True
        if c in "{[":
            self.stack.append(c)
            self.expect_key, self.after_colon, self.empty = c == "{", False, True
            out.append(c)
            return
        self.empty = False
        if c in "}]":
            if self.after_colon:
                out.append("null")
            elif self.key_done:
                out.append(": null")
            # Emit the closer that matches what is open, so {"a": [1, 2}] heals.
            out.append(_CLOSER[self.stack.pop()])
            self.expect_key = self.after_colon = self.key_done = False
            self.value_done = True
            if not self.stack:
                self.done = True
        elif c in "\"'":
            self.in_string = True
            self.quote = c
            self.string_is_key = self.expect_key
            self.expect_key = self.after_colon = False
            out.append('"')
        elif c == ":":
            self.expect_key = self.key_done = False
            self.after_colon = True
            out.append(c)
        elif self.expect_key:
            self.key = c
            self.expect_key = False
        else:
            self.word = c
            self.after_colon = False


class StreamProcessor:
    """Preprocess (think/fence stripping) + live repair, keeping the raw text."""

    def __init__(self, json_only: bool = False):
        self._pre = PreprocessState()
        self._rep = StreamRepairer(json_only=json_only)
        self._raw: list[str] = []
        self._finished = False

    def feed(self, chunk: str) -> str:
        self._raw.append(chunk)
        return self._rep.feed(preprocess_chunk(chunk, self._pre))

    def finish(self) -> str:
        """Flush and close. Safe to call more than once; later calls return ""."""
        if self._finished:
            return ""
        self._finished = True
        tail = self._rep.feed(preprocess_finalize(self._pre)) + self._rep.finish()
        if self._rep.json_only and not self._rep.emitted_json and sum(map(len, self._raw)) <= 100_000:
            # Nothing JSON-shaped got through live (e.g. an unclosed <think> hid it);
            # fall back to the full repair so the client still gets a value.
            final = self.result()
            if final.ok and final.text:
                tail += final.text
        return tail

    @property
    def raw_text(self) -> str:
        return "".join(self._raw)

    def result(self) -> RepairResult:
        """Authoritative final value: the full non-streaming repair of the raw text."""
        return repair(self.raw_text)
