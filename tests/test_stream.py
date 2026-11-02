import json

import pytest

from streamfix import StreamProcessor, StreamRepairer, repair

from cases import REPAIR_CASES

STREAMABLE = [c for c in REPAIR_CASES if len(c[1]) < 5000]


def run(text, size, json_only=True):
    proc = StreamProcessor(json_only=json_only)
    out = "".join(proc.feed(text[i:i + size]) for i in range(0, len(text), size))
    return out + proc.finish(), proc


@pytest.mark.parametrize("name,text,expected", STREAMABLE, ids=[c[0] for c in STREAMABLE])
@pytest.mark.parametrize("size", [1, 3, 7])
def test_stream_matches_full_repair(name, text, expected, size):
    out, proc = run(text, size)
    assert json.loads(out) == repair(text).value


def test_passthrough_keeps_prose_and_repairs_json():
    out, _ = run("Here's what you asked for:\n{name: 'Ann', tags: ['a',],}\nCheers!", 2, json_only=False)
    assert out.startswith("Here's what you asked for:\n")
    assert out.endswith("\nCheers!")
    start, end = out.index("{"), out.rindex("}") + 1
    assert json.loads(out[start:end]) == {"name": "Ann", "tags": ["a"]}


def test_commas_stay_in_place():
    text = '{\n  "a": 1,\n  "b": [\n    2,\n    3,\n  ]\n}'
    out, _ = run(text, 1)
    assert "\n  ," not in out and '"a": 1,\n' in out
    assert json.loads(out) == {"a": 1, "b": [2, 3]}


def test_truncated_stream_is_closed():
    out, _ = run('{"items": [{"id": 1}, {"id": 2, "name": "Bo', 4)
    assert json.loads(out) == {"items": [{"id": 1}, {"id": 2, "name": "Bo"}]}


def test_words_split_across_chunks():
    rep = StreamRepairer()
    out = "".join(rep.feed(c) for c in ['{"ok": Tr', 'ue, "v": No', "ne, s: in prog", "ress}"]) + rep.finish()
    assert json.loads(out) == {"ok": True, "v": None, "s": "in progress"}


def test_unclosed_think_falls_back_to_full_repair():
    out, _ = run('<think>partial{"x": 1}', 2)
    assert json.loads(out) == {"x": 1}


def test_finish_is_idempotent():
    proc = StreamProcessor(json_only=True)
    first = proc.feed('<think>{"x": 1}') + proc.finish()
    assert proc.finish() == ""
    assert json.loads(first) == {"x": 1}


def test_result_is_authoritative():
    _, proc = run('```json\n{"a": [1, 2}]\n```', 3)
    assert proc.result().value == {"a": [1, 2]}


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1 // one\n}', {"a": 1}),
    ("[1 /* c */, 2]", [1, 2]),
    ('{"a": `x`}', {"a": "`x`"}),
    ('{"a": 1;}', {"a": "1;"}),
    ('["a": 1]', ["a", 1]),
    ('{"x": 4 / 2, "u": http://x.com/a}', {"x": "4 / 2", "u": "http://x.com/a"}),
])
def test_stray_characters_never_leak(text, expected):
    out, _ = run(text, 1)
    assert json.loads(out) == expected
