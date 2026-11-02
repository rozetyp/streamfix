from streamfix.contract import check_schema, coerce_types, retry_prompt, validate

PERSON = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer", "minimum": 0},
                   "active": {"type": "boolean"}, "score": {"type": ["number", "null"]}},
    "required": ["name", "age"],
}


def test_coercion():
    value, notes = coerce_types({"name": "Bo", "age": "42", "active": "yes", "score": "1.5"}, PERSON)
    assert value == {"name": "Bo", "age": 42, "active": True, "score": 1.5}
    assert len(notes) == 3


def test_coercion_leaves_unconvertible_values():
    value, notes = coerce_types({"name": "Bo", "age": "forty"}, PERSON)
    assert value["age"] == "forty" and notes == []
    assert validate(value, PERSON)


def test_float_to_int_and_nested_arrays():
    schema = {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer"}}}}
    assert coerce_types([{"n": 3.0}, {"n": "4"}], schema)[0] == [{"n": 3}, {"n": 4}]


def test_validate_reports_paths():
    errors = validate({"name": "Bo", "age": -1}, PERSON)
    assert errors and "age" in errors[0]


def test_valid_value():
    assert validate({"name": "Bo", "age": 1}, PERSON) == []


def test_check_schema():
    assert check_schema(PERSON) is None
    assert check_schema({"type": "nonsense"}) is not None


def test_retry_prompt_mentions_requirements():
    prompt = retry_prompt(["at root: 'age' is a required property"], PERSON)
    assert "age" in prompt and "Required fields: name, age" in prompt


def test_coercion_edge_cases():
    schema = {"type": "object", "properties": {
        "i": {"type": "integer"}, "n": {"type": "number"}, "u": {"type": ["integer", "boolean"]},
        "x": {"type": "number"}, "nested": {"properties": {"k": {"type": "integer"}}}}}
    value, _ = coerce_types({"i": "42.0", "n": "1e3", "u": "true", "x": "nan", "nested": {"k": "7"}}, schema)
    assert value == {"i": 42, "n": 1000.0, "u": True, "x": "nan", "nested": {"k": 7}}


def test_boolean_subschemas_do_not_crash():
    schema = {"type": "object", "properties": {"a": True, "b": {"type": "integer"}}, "required": ["a"]}
    assert coerce_types({"a": "1", "b": "2"}, schema)[0] == {"a": "1", "b": 2}
    assert "a: any" in retry_prompt(["at root: 'a' is a required property"], schema)


def test_values_that_already_fit_are_left_alone():
    schema = {"type": "object", "properties": {"zip": {"type": ["string", "integer"]}, "big": {"type": "integer"}}}
    value, notes = coerce_types({"zip": "02134", "big": "9007199254740993"}, schema)
    assert value == {"zip": "02134", "big": 9007199254740993} and len(notes) == 1
