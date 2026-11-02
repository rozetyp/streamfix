"""Real model responses that broke json.loads (see data/real_outputs.json)."""
import json
from pathlib import Path

import pytest

from streamfix import repair, repair_tool_arguments

CASES = json.loads((Path(__file__).parent / "data" / "real_outputs.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[f"{c['model'].split('/')[-1]}-{i}" for i, c in enumerate(CASES)])
def test_real_output_repairs(case):
    if case["kind"] == "tool_args":
        fixed, _ = repair_tool_arguments(case["raw"])
        json.loads(fixed)
    else:
        assert repair(case["raw"]).ok


def test_kimi_headless_think_returns_final_answer():
    # Kimi sends reasoning (with a draft JSON) and only a closing </think>.
    raw = next(c["raw"] for c in CASES if "Jane Doe" in c["raw"] and "</think>" in c["raw"] and "John Doe" in c["raw"])
    assert repair(raw).value["name"] == "Jane Doe"


def test_llama_curly_quote_tool_args_keep_their_fields():
    raws = [c["raw"] for c in CASES if c["kind"] == "tool_args" and "“city”" in c["raw"]]
    addresses = [json.loads(repair_tool_arguments(raw)[0])["shipping_address"] for raw in raws]
    # one sample has the model's own garbage inside the street value; the rest are clean
    clean = [a for a in addresses if a["street"] == "42 Oak Street"]
    assert len(clean) >= 3 and all(a["city"] == "Austin" for a in clean)
