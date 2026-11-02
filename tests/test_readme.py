"""The comparison numbers in README.md and the project page are computed here, so they cannot drift."""
import json
import re
from pathlib import Path

import json_repair

from streamfix import StreamProcessor, repair

from cases import ANY, REPAIR_CASES

ROOT = Path(__file__).resolve().parent.parent
CORPUS = json.loads((ROOT / "tests/data/json_repair_cases.json").read_text())["cases"]
NONE = object()


def streamfix(text):
    r = repair(text)
    return r.value if r.ok else NONE


def json_repair_only(text):
    v = json_repair.repair_json(text, return_objects=True)
    return NONE if v == "" else v


def streamed(text):
    p = StreamProcessor(json_only=True)
    try:
        return json.loads("".join(p.feed(c) for c in text) + p.finish())
    except ValueError:
        return NONE


def hit(value, expected):
    return value is not NONE and (expected is ANY or value == expected)


def corpus_score(fn):
    return sum((fn(c["input"]) is NONE) if c.get("no_json") else hit(fn(c["input"]), c["expected"])
               for c in CORPUS)


def cases_score(fn, cases):
    return sum(hit(fn(text), expected) for _, text, expected in cases)


def scores():
    streamable = [c for c in REPAIR_CASES if len(c[1]) < 5000]
    return {
        "corpus": (corpus_score(json_repair_only), corpus_score(streamfix), len(CORPUS)),
        "cases": (cases_score(json_repair_only, REPAIR_CASES), cases_score(streamfix, REPAIR_CASES),
                  len(REPAIR_CASES)),
        "streamed": (cases_score(streamed, streamable), len(streamable)),
    }


def table():
    s = scores()
    (jc, sc, n), (jm, sm, m), (st, k) = s["corpus"], s["cases"], s["streamed"]
    return [
        f"| json-repair's own test suite ({n} cases) | {jc} | **{sc}** |",
        f"| StreamFix's cases ({m}) | {jm} | **{sm}** |",
        f"| StreamFix's cases, streamed one character at a time ({k}) | n/a | **{st}** |",
    ]


def test_readme_table_matches_measurements():
    readme = (ROOT / "README.md").read_text()
    rows = [l for l in readme.splitlines() if re.match(r"\| (json-repair's own|StreamFix's cases)", l)]
    assert rows == table(), "README table is stale; paste in:\n" + "\n".join(table())


def test_page_numbers_match_measurements():
    page = (ROOT / "src/streamfix/index.html").read_text()
    s = scores()
    (jc, sc, n), (jm, sm, m), (st, k) = s["corpus"], s["cases"], s["streamed"]
    for cell in (f"{jc} / {n}", f"{sc} / {n}", f"{jm} / {m}", f"{sm} / {m}", f"{st} / {k}"):
        assert f'<td class="n">{cell}</td>' in page, f"page table is stale: missing {cell}"
