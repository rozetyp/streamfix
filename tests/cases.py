"""Shared repair cases: (name, model output, expected value).

`ANY` means "must produce some valid JSON" where the exact value is a judgment call.
"""
ANY = object()

REPAIR_CASES = [
    # basic syntax
    ("trailing comma object", '{"a": 1, "b": 2,}', {"a": 1, "b": 2}),
    ("trailing comma array", "[1, 2, 3,]", [1, 2, 3]),
    ("single quotes", "{'key': 'value'}", {"key": "value"}),
    ("unquoted keys", '{name: "Alice", age: 30}', {"name": "Alice", "age": 30}),
    ("mixed defects", '{name: "Bob", age: 25,}', {"name": "Bob", "age": 25}),
    ("js comments", '{"a": 1, // comment\n"b": 2}', {"a": 1, "b": 2}),
    ("double commas", "[1,, 2, 3]", [1, 2, 3]),
    ("unquoted string value", '{"status": active}', {"status": "active"}),
    ("mismatched brackets", '{"a": [1, 2}]', {"a": [1, 2]}),
    # fences
    ("basic fence", '```json\n{"a": 1}\n```', {"a": 1}),
    ("fence with other lang tag", '```javascript\n{"a": 1}\n```', {"a": 1}),
    ("fence never closed", '```json\n{"a": 1}', {"a": 1}),
    ("double fenced", '```json\n```json\n{"a": 1}\n```\n```', {"a": 1}),
    ("four backticks", '````{ "key": "value" }```', {"key": "value"}),
    ("backticks inside a string", '{"key": "```json"}', {"key": "```json"}),
    # reasoning blocks
    ("think + JSON", '<think>reasoning</think>{"result": 42}', {"result": 42}),
    ("think + newlines", '<think>\nstep 1\n</think>\n{"x": 1}', {"x": 1}),
    ("unclosed think", '<think>partial{"x": 1}', {"x": 1}),
    ("think containing JSON-like text",
     '<think>I could return {"name": "draft"} but better add age</think>{"name": "Alice", "age": 30}',
     {"name": "Alice", "age": 30}),
    ("think containing arrays", '<think>options: [1,2] or [3]</think>\n```json\n{"pick": [3]}\n```', {"pick": [3]}),
    # prose
    ("prose before", 'Here is the result: {"name": "Alice"}', {"name": "Alice"}),
    ("prose around", 'Result: {"name": "Alice"} Hope this helps!', {"name": "Alice"}),
    ("multiline prose", 'Let me think.\nOk:\n{"answer": 42}\nDone!', {"answer": 42}),
    ("prose with apostrophe", "Here's the JSON:\n{\"a\": \"it's\"}", {"a": "it's"}),
    # truncation
    ("truncated object", '{"a": 1, "b": 2', {"a": 1, "b": 2}),
    ("truncated nested", '{"a": {"b": [1, 2', {"a": {"b": [1, 2]}}),
    ("truncated string", '{"name": "Ali', {"name": "Ali"}),
    ("deep truncation", '{"a":{"b":{"c":{"d":[1,2,3', {"a": {"b": {"c": {"d": [1, 2, 3]}}}}),
    ("truncated after colon", '{"key": ', {"key": None}),
    ("truncated nested after colon", '{"a": {"b": ', {"a": {"b": None}}),
    ("truncated inside nested object", '{"incomplete": "object", "array": [1, 2, {"nested": true',
     {"incomplete": "object", "array": [1, 2, {"nested": True}]}),
    ("partial true", '{"valid": tru', {"valid": True}),
    ("partial false", '{"valid": fals', {"valid": False}),
    ("partial null", '{"value": nul', {"value": None}),
    ("truncated empty object", "{", {}),
    ("truncated empty array", "[", []),
    # python literals and numbers
    ("None", '{"value": None}', {"value": None}),
    ("True/False", '{"a": True, "b": False}', {"a": True, "b": False}),
    ("literals in array", "[True, False, None, 1]", [True, False, None, 1]),
    ("literal inside string kept", '{"msg": "True story"}', {"msg": "True story"}),
    ("leading zeros", '{"a": 007, "b": 042}', {"a": 7, "b": 42}),
    ("zero kept", '{"n": 0}', {"n": 0}),
    ("decimal kept", '{"n": 0.5}', {"n": 0.5}),
    ("zeros inside string kept", '{"code": "007"}', {"code": "007"}),
    # misc
    ("unicode", '{"名前": "太郎", "mood": "😀"}', {"名前": "太郎", "mood": "😀"}),
    ("BOM prefix", '﻿{"a": 1}', {"a": 1}),
    ("extra closing brace", '{"a": 1}}', {"a": 1}),
    ("1000 keys", "{" + ",".join(f'"k{i}":{i}' for i in range(1000)) + "}",
     {f"k{i}": i for i in range(1000)}),
    # real-world shapes
    ("fence + explanation", '```json\n{"answer": 42}\n```\n\nHere you go.', {"answer": 42}),
    ("think + fence", '<think>6*7=42</think>\n```json\n{"answer": 42}\n```', {"answer": 42}),
    ("prose + fence + prose", 'Sure!\n```\n{"name": "Alice"}\n```\nLet me know.', {"name": "Alice"}),
    ("DeepSeek style", '<think>\nok\n</think>\n```json\n{"name": "Bob"}\n```', {"name": "Bob"}),
    ("array response", 'Results:\n[{"id": 1}, {"id": 2}]', [{"id": 1}, {"id": 2}]),
    ("fence + trailing commas", '```json\n{"items": ["a", "b",], "count": 2,}\n```', {"items": ["a", "b"], "count": 2}),
    ("fence + quotes + comma", "```json\n{'items': ['a', 'b',],}\n```", {"items": ["a", "b"]}),
    ("think + unquoted + comma", '<think>go</think>\n{name: "Alice", age: 30,}', {"name": "Alice", "age": 30}),
    ("prose + fence + truncation", 'Here:\n```json\n{"status": "ok", "items": [1, 2', {"status": "ok", "items": [1, 2]}),
    ("all defects", "<think>ok</think>\n```json\n{name: 'Alice', age: 30, hobbies: ['reading',],}\n```",
     {"name": "Alice", "age": 30, "hobbies": ["reading"]}),
    ("think + python + comma", '<think>done</think>{"valid": True, "value": None, "items": [1,],}',
     {"valid": True, "value": None, "items": [1]}),
    ("fence + zeros + python", '```json\n{"code": 007, "active": True}\n```', {"code": 7, "active": True}),
    ("forced broken output (live test 2026-11-01)",
     "```json\n{name: 'Alice', age: 30, active: True, scores: [1,2,3,], note: None,}\n```",
     {"name": "Alice", "age": 30, "active": True, "scores": [1, 2, 3], "note": None}),
]

NO_JSON_CASES = ["", "   \n\t  ", "Just plain text, no JSON here."]


# Longer model-shaped outputs. The "*_split" ones contain tags broken by a newline,
# as some models emit them.
FIXTURES = [
    ('simple_fenced', '```json\n{"name": "John", "age": 30}\n```'),
    ('think_block', '<think>\nThe user wants a JSON response. I need to provide a clean object with their requested data.\n</think>\n\n{"result": "success", "data": [1, 2, 3]}'),
    ('truncated', '{"incomplete": "object", "array": [1, 2, {"nested": true'),
    ('nested', '{\n  "user": {\n    "name": "Alice",\n    "preferences": {\n      "theme": "dark",\n      "notifications": [\n        {"type": "email", "enabled": true},\n        {"type": "push", "enabled": false}\n      ]\n    }\n  },\n  "metadata": {\n    "version": "2.1",\n    "tags": ["important", "user-data"]\n  }\n}'),
    ('escaped_content', '{"message": "Say \\"hello\\" and use \\\\backslash", "path": "C:\\\\Users\\\\test\\\\file.txt", "json_like": "{\\"key\\": \\"value\\"}"}'),
    ('mixed_content', 'Sure! Here\'s the JSON you requested:\n\n```json\n{"response": "clean", "status": "success"}\n```\n\nHope this helps!'),
    ('array_root', '[\n  {"id": 1, "name": "First"},\n  {"id": 2, "name": "Second"},\n  {"id": 3, "name": "Third"}\n]'),
    ('deepseek_reasoning', '<think>\nUser wants a list of programming languages. I should return them in a structured format.\nThe best approach is to use a JSON array with objects containing name and category.\n</think>\n\n```json\n{\n  "languages": [\n    {"name": "Python", "category": "scripting"},\n    {"name": "JavaScript", "category": "web"},\n    {"name": "Rust", "category": "systems"}\n  ]\n}\n```'),
    ('trailing_commas', '{\n  "items": [\n    {"id": 1, "active": true,},\n    {"id": 2, "active": false,}\n  ],\n  "total": 2,\n}'),
    ('multiple_thinks', '<think>\nFirst I need to understand what the user wants.\n</think>\n\nLet me process this request.\n\n<think>\nActually, I should return a JSON response with the analysis results.\n</think>\n\n{"analysis": "complete", "confidence": 0.95}'),
    ('large_nested', '{\n  "data": [\n    {\n      "id": "user_001",\n      "profile": {\n        "name": "Complex User",\n        "settings": {\n          "preferences": ["dark_mode", "notifications"],\n          "permissions": {\n            "read": true,\n            "write": false,\n            "admin": false\n          }\n        }\n      },\n      "activity": [\n        {"timestamp": "2024-01-01", "action": "login"},\n        {"timestamp": "2024-01-02", "action": "update_profile"}\n      ]\n    }\n  ],\n  "metadata": {\n    "total_count": 1,\n    "page": 1,\n    "has_more": false\n  }\n}'),
    ('no_json', 'This is just plain text without any JSON structure.\nNo brackets or braces here.\nJust regular content that should not match the FSM.'),
    ('unfenced_mixed', 'I\'ll provide the response data now:\n\n{"status": "success", "message": "Operation completed"}\n\nThis should work without fences too.'),
    ('complex_think', '<think>\nThe user is asking for a complex data structure. Let me think about this:\n\n1. They need user information\n2. They want it in JSON format  \n3. I should include nested objects and arrays\n4. Let me structure this properly:\n   - User object with id, name, email\n   - Preferences object with settings\n   - Array of recent activities\n\nActually, let me reconsider the structure...\n</think>\n\nHere\'s the JSON response you requested:\n\n```json\n{\n  "user": {\n    "id": "12345",\n    "name": "Jane Doe",\n    "email": "jane@example.com",\n    "preferences": {\n      "theme": "light",\n      "language": "en",\n      "notifications": {\n        "email": true,\n        "push": false,\n        "sms": false\n      }\n    },\n    "recent_activity": [\n      {"action": "login", "timestamp": "2024-01-15T10:30:00Z"},\n      {"action": "profile_update", "timestamp": "2024-01-15T11:15:00Z"}\n    ]\n  }\n}\n```'),
    ('embedded_json', '{\n  "description": "This field contains JSON-like text: {\\"key\\": \\"value\\"} but it\'s just a string",\n  "example_code": "const obj = {name: \'test\', value: 42};",\n  "valid_json": true\n}'),
    ('think_split', '<thi\nnk>\nSome reasoning here\n</think>\n\n{"boundary": "test1"}'),
    ('fence_split', '``\n`json\n{"boundary": "test2"}\n```'),
    ('close_think_split', '<think>\nSome reasoning\n</thin\nk>\n\n{"boundary": "test3"}'),
    ('close_fence_split', '```json\n{"boundary": "test4"}\n``\n`'),
    ('json_split', '<think>\nReasoning here\n</think>\n\n{"boundary": "test\n5", "split": tr\nue}'),
]
