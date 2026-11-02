"""Score StreamFix on json-repair's own test cases (see data/json_repair_cases.json).

These cases were written for json-repair, so they reflect its conventions; StreamFix
is expected to give the same answer on every one.
"""
import json
from pathlib import Path

import pytest

from streamfix import repair

DATA = json.loads((Path(__file__).parent / "data" / "json_repair_cases.json").read_text())
CASES = DATA["cases"]



@pytest.mark.parametrize("case", CASES, ids=[f"{c['source']}:{i}" for i, c in enumerate(CASES)])
def test_case(case):
    result = repair(case["input"])
    if case.get("no_json"):
        assert not result.ok
    else:
        assert result.ok and result.value == case["expected"]
