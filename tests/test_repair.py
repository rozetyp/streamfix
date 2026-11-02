import json

import pytest

from streamfix import repair, repair_tool_arguments
from streamfix.cli import main
from streamfix.extract import PreprocessState, extract, preprocess, preprocess_chunk, preprocess_finalize, preprocess_result

from cases import ANY, FIXTURES, NO_JSON_CASES, REPAIR_CASES


@pytest.mark.parametrize("name,text,expected", REPAIR_CASES, ids=[c[0] for c in REPAIR_CASES])
def test_repair(name, text, expected):
    result = repair(text)
    assert result.ok, f"{name}: no JSON recovered"
    if expected is not ANY:
        assert result.value == expected
    assert json.loads(result.text) == result.value


@pytest.mark.parametrize("text", NO_JSON_CASES)
def test_no_json(text):
    result = repair(text)
    assert not result.ok and result.status == "failed"


def test_valid_json_is_a_pass():
    result = repair('{"a": [1, 2], "b": null}')
    assert result.status == "pass" and result.repairs == []


def test_repairs_are_reported():
    result = repair('<think>x</think>\n```json\n{"a": True, "b": [1,],}\n```')
    assert result.status == "repaired"
    assert {"think_strip", "fence_strip", "remove_trailing_comma", "fix_python_literals"} <= set(result.repairs)


def test_deep_nesting_does_not_crash():
    result = repair('{"a":' * 1000 + "1" + "}" * 1000)
    assert result.status in ("pass", "repaired", "failed")


@pytest.mark.parametrize("args,expected,changed", [
    ('{"query": "hello"}', {"query": "hello"}, False),
    ('{"query": "hel', {"query": "hel"}, True),
    ("{'query': 'hello'}", {"query": "hello"}, True),
    ('{"query": "hello",}', {"query": "hello"}, True),
    ('```json\n{"query": "hello"}\n```', {"query": "hello"}, True),
    ('{"valid": True, "count": None}', {"valid": True, "count": None}, True),
])
def test_tool_arguments(args, expected, changed):
    fixed, was_repaired = repair_tool_arguments(args)
    assert json.loads(fixed) == expected and was_repaired == changed


# --- preprocessing gives the same answer however the text is chunked ---

def preprocess_chunks(chunks):
    state = PreprocessState()
    for chunk in chunks:
        preprocess_chunk(chunk, state)
    preprocess_finalize(state)
    return preprocess_result(state)


@pytest.mark.parametrize("name,text", FIXTURES, ids=[f[0] for f in FIXTURES])
@pytest.mark.parametrize("size", [1, 2, 3, 8, 64])
def test_chunking_parity(name, text, size):
    chunks = [text[i:i + size] for i in range(0, len(text), size)]
    assert preprocess_chunks(chunks) == preprocess(text)


@pytest.mark.parametrize("name,text", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_fixtures_repair(name, text):
    assert repair(text).ok == (name != "no_json")


def test_split_tags_are_stripped():
    chunks = ["<th", "ink>reason", "ing</thi", 'nk>{"x": 1}']
    assert preprocess_chunks(chunks) == '{"x": 1}'
    assert preprocess_chunks(["```js", 'on\n{"a"', ": 1}\n```"]).strip() == '{"a": 1}'


def test_extract_reports_provenance():
    ex = extract('<think>t</think>Sure:\n```json\n{"a": 1}\n```')
    assert ex.think_strip and ex.fence_strip and ex.spans[0].status == "DONE"


@pytest.mark.parametrize("text,expected", [
    # prose after the JSON that happens to contain brackets
    ('{"name": "Alice"}\n\nNote: you can replace {name} with any value.', {"name": "Alice"}),
    ('{"a": 1}\nI set "a" (see [docs]).', {"a": 1}),
    # a format example in the prose before the real answer
    ('Use the format {key: value}. Here it is:\n{"ok": true}', {"ok": True}),
    # a draft, then the answer that got cut off
    ('Draft: {"a": 0}. Final: {"a": 1, "b": [1, 2', {"a": 1, "b": [1, 2]}),
    # glued-together values keep json-repair's convention
    ('{"key": "value"}, "key2": "value2"}', {"key": "value", "key2": "value2"}),
    # reasoning with only a closing tag (Kimi)
    ('Drafting {"id": 9} first.</think> {"id": 1}', {"id": 1}),
    # curly quotes used as delimiters, but not inside prose
    ('{"street": "42 Oak Street”, “city”: “Austin”}', {"street": "42 Oak Street", "city": "Austin"}),
    ('{"quote": "He said “hi” to me"}', {"quote": "He said “hi” to me"}),
    # strings that contain fence or think markers
    ('```json\n{"md": "Wrap code in ``` fences"}\n```', {"md": "Wrap code in ``` fences"}),
    ('```json\n{"html": "use <think> tags"}\n```', {"html": "use <think> tags"}),
    ('<think>maybe ```json</think>\n```json\n{"a": 1}\n```\nThen {b}', {"a": 1}),
    # NaN / Infinity are not JSON
    ('{"a": NaN, "b": [Infinity, 1]}', {"a": None, "b": [None, 1]}),
    # second review: span choice
    ('{"lo": 0, "hi": 10}\nNote: the interval is [0, 10).', {"lo": 0, "hi": 10}),
    ('{"emoji": "smile"}\nSorry :-[ I could not do more.', {"emoji": "smile"}),
    ('Given the input [3, 1, 2], the sorted output is:\n[1, 2, 3]', [1, 2, 3]),
    ('Original: {"a": 1}\nCorrected: {"a": 2}', {"a": 2}),
    ('{"status": "ok"}\n\nFor reference, the schema is {"type": "object", "required": ["status"]}', {"status": "ok"}),
    # second review: markers in strings and stray quotes in prose
    ('```json\n{"tag": "</think>"}\n```', {"tag": "</think>"}),
    ('Input was [1, 2, 3] (a 3" list).\n```json\n[4]\n```', [4]),
])
def test_review_regressions(text, expected):
    assert repair(text).value == expected


@pytest.mark.parametrize("text", ['{"a":' * 300, "[" * 500, '{"a":' * 5000 + "1" + "}" * 5000])
def test_pathological_nesting_never_raises(text):
    repair(text)


def test_cli_repair(tmp_path, capsys):
    f = tmp_path / "out.txt"
    f.write_text("<think>x</think>```json\n{a: True,}\n```")
    assert main(["repair", str(f), "--report"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"a": True} and "repaired" in captured.err
    f.write_text("no json here")
    assert main(["repair", str(f)]) == 1
    assert main(["repair", str(tmp_path / "missing.txt")]) == 1
