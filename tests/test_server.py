"""Proxy tests against a fake upstream (httpx.MockTransport); no network or keys."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from streamfix.server import create_app


def completion(content=None, tool_args=None, model="m"):
    message = {"role": "assistant", "content": content}
    if tool_args is not None:
        message["tool_calls"] = [{"id": "c1", "type": "function",
                                  "function": {"name": "search", "arguments": tool_args}}]
    return {"id": "x", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


def sse(*contents, finish=True):
    lines = [f'data: {json.dumps({"id": "s", "model": "m", "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}]})}'
             for c in contents]
    if finish:
        lines.append(f'data: {json.dumps({"id": "s", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})}')
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


class Upstream:
    """Fake upstream: returns queued responses and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        status, body = self.responses.pop(0)
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(status, json=body)


def client_for(upstream, **kw):
    app = create_app(upstream_url="http://upstream/v1", upstream_key="sk-test",
                     transport=httpx.MockTransport(upstream), **kw)
    return TestClient(app)


def post(client, body, **headers):
    return client.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}], **body},
                       headers=headers)


def test_repairs_content_and_reports_it():
    up = Upstream((200, completion("```json\n{name: 'Alice', active: True, scores: [1,2,],}\n```")))
    with client_for(up) as c:
        r = post(c, {})
    assert r.status_code == 200
    assert json.loads(r.json()["choices"][0]["message"]["content"]) == {"name": "Alice", "active": True, "scores": [1, 2]}
    assert r.headers["x-streamfix-status"] == "repaired"
    assert "fence_strip" in r.headers["x-streamfix-applied"]


def test_valid_json_and_plain_text_pass_through():
    up = Upstream((200, completion('{"a": 1}')), (200, completion("Hello there, no JSON.")))
    with client_for(up) as c:
        ok, prose = post(c, {}), post(c, {})
    assert ok.headers["x-streamfix-status"] == "pass"
    assert prose.json()["choices"][0]["message"]["content"] == "Hello there, no JSON."
    assert prose.headers["x-streamfix-status"] == "failed"


def test_forwards_caller_key_or_uses_configured_one():
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json=completion('{"a": 1}'))

    with client_for(handler) as c:
        post(c, {}, Authorization="Bearer caller-key")
        post(c, {})
    assert seen == ["Bearer caller-key", "Bearer sk-test"]


def test_tool_call_arguments_are_repaired():
    up = Upstream((200, completion(None, tool_args="{'q': 'cats', limit: 5,}")))
    with client_for(up) as c:
        r = post(c, {})
    args = r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args) == {"q": "cats", "limit": 5}
    assert r.headers["x-streamfix-tool-args-repaired"] == "1"
    assert r.headers["x-streamfix-status"] == "repaired"


def test_contract_mode_coerces_and_validates():
    schema = {"type": "object", "properties": {"age": {"type": "integer"}}, "required": ["age"]}
    up = Upstream((200, completion('```json\n{"age": "42"}\n```')))
    with client_for(up) as c:
        r = post(c, {"schema": schema})
    assert json.loads(r.json()["choices"][0]["message"]["content"]) == {"age": 42}
    assert r.headers["x-streamfix-schema-valid"] == "true"
    assert "schema" not in up.requests[0]


def test_contract_mode_retries_once_with_correction():
    schema = {"type": "object", "properties": {"age": {"type": "integer"}}, "required": ["age"]}
    up = Upstream((200, completion('{"name": "Bo"}')), (200, completion('{"age": 7}')))
    with client_for(up) as c:
        r = post(c, {"schema": schema})
    assert r.headers["x-streamfix-retry-count"] == "1"
    assert r.headers["x-streamfix-schema-valid"] == "true"
    retry_messages = up.requests[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": '{"name": "Bo"}'}
    assert "required" in retry_messages[-1]["content"]


def test_contract_mode_gives_up_after_retry():
    schema = {"type": "object", "required": ["age"]}
    up = Upstream((200, completion('{"x": 1}')), (200, completion('{"y": 2}')))
    with client_for(up) as c:
        r = post(c, {"schema": schema})
    assert r.headers["x-streamfix-schema-valid"] == "false" and len(up.requests) == 2


def test_strict_mode_returns_422():
    up = Upstream((200, completion("I can't produce JSON for that.")))
    with client_for(up) as c:
        r = post(c, {}, **{"X-StreamFix-Strict": "true"})
    assert r.status_code == 422 and r.json()["error"]["type"] == "json_unrecoverable"


def test_repair_off_leaves_content_alone():
    up = Upstream((200, completion("{a: 1,}")))
    with client_for(up) as c:
        r = post(c, {}, **{"X-StreamFix-Repair": "off"})
    assert r.json()["choices"][0]["message"]["content"] == "{a: 1,}"


def test_invalid_schema_and_stream_conflicts_are_400():
    with client_for(Upstream()) as c:
        assert post(c, {"schema": {"type": "nonsense"}}).status_code == 400
        assert post(c, {"stream": True, "schema": {"type": "object"}}).status_code == 400


def test_upstream_client_error_is_passed_back():
    with client_for(Upstream((401, {"error": "bad key"}))) as c:
        assert post(c, {}).status_code == 401


def test_fallback_model_on_server_error():
    up = Upstream((503, {"error": "down"}), (200, completion('{"ok": true}', model="backup")))
    with client_for(up, fallback_models=["backup"]) as c:
        r = post(c, {})
    assert r.status_code == 200 and r.headers["x-streamfix-model"] == "backup"
    assert [q["model"] for q in up.requests] == ["m", "backup"]


def stream_text(response):
    text = ""
    for line in response.text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            for choice in json.loads(line[6:])["choices"]:
                text += choice.get("delta", {}).get("content") or ""
    return text


def test_streaming_json_mode():
    up = Upstream((200, sse("Here's the data:\n```json\n{na", "me: 'Al", "ice', ok: Tru", "e, n: [1,", "2,],}\n```\nBye")))
    with client_for(up) as c:
        r = post(c, {"stream": True}, **{"X-StreamFix-Stream-Mode": "json"})
    assert r.headers["x-streamfix-stream-mode"] == "json"
    assert json.loads(stream_text(r)) == {"name": "Alice", "ok": True, "n": [1, 2]}
    assert r.text.rstrip().endswith("data: [DONE]")


def test_streaming_passthrough_closes_truncated_json_before_finish():
    up = Upstream((200, sse("Sure: ", '{"items": [1, 2', ", 3")))
    with client_for(up) as c:
        r = post(c, {"stream": True})
    text = stream_text(r)
    assert text.startswith("Sure: ")
    assert json.loads(text[text.index("{"):]) == {"items": [1, 2, 3]}
    # the closing tail must arrive before the chunk that carries finish_reason
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: {")]
    finish_at = next(i for i, e in enumerate(events) if e["choices"][0].get("finish_reason"))
    assert "]}" in events[finish_at - 1]["choices"][0]["delta"]["content"]


def test_body_limit_and_home_page(monkeypatch):
    monkeypatch.setenv("STREAMFIX_MAX_BODY_BYTES", "200")
    with client_for(Upstream()) as c:
        r = post(c, {"messages": [{"role": "user", "content": "x" * 500}]})
        assert r.status_code == 413
        home = c.get("/")
        assert home.status_code == 200 and "<title>StreamFix</title>" in home.text


def test_no_key_anywhere_is_401():
    app = create_app(upstream_url="http://upstream/v1", upstream_key=None,
                     transport=httpx.MockTransport(Upstream()))
    with TestClient(app) as c:
        assert post(c, {}).status_code == 401


def test_health():
    with client_for(Upstream()) as c:
        assert c.get("/health").json()["status"] == "ok"


# --- regressions from review ---

def test_non_json_upstream_is_502():
    def handler(request):
        return httpx.Response(200, text="<html>gateway</html>")
    with client_for(handler) as c:
        assert post(c, {}).status_code == 502


def test_odd_message_shapes_do_not_crash():
    weird = completion(None)
    weird["choices"][0]["message"]["content"] = [{"type": "text", "text": "{a: 1}"}]
    weird["choices"][0]["message"]["tool_calls"] = [{"function": {"name": "f", "arguments": {"a": 1}}}]
    with client_for(Upstream((200, weird))) as c:
        r = post(c, {})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == {"a": 1}


def test_client_errors_pass_through_even_with_fallbacks():
    up = Upstream((401, {"error": "bad key"}))
    with client_for(up, fallback_models=["backup"]) as c:
        assert post(c, {}).status_code == 401
    assert len(up.requests) == 1  # a bad key fails the same way on every model


def test_rate_limit_status_is_kept():
    with client_for(Upstream((429, {"error": "slow down"}))) as c:
        assert post(c, {}).status_code == 429


def test_schema_ignored_when_repair_is_off():
    up = Upstream((200, completion("not json at all")))
    with client_for(up) as c:
        r = post(c, {"schema": {"type": "object"}}, **{"X-StreamFix-Repair": "off"})
    assert "x-streamfix-schema-valid" not in r.headers


def test_every_choice_is_repaired():
    data = completion("{a: 1,}")
    data["choices"].append({"index": 1, "message": {"role": "assistant", "content": "{b: 2,}"}, "finish_reason": "stop"})
    with client_for(Upstream((200, data))) as c:
        r = post(c, {})
    assert [json.loads(ch["message"]["content"]) for ch in r.json()["choices"]] == [{"a": 1}, {"b": 2}]


def sse_events(*events):
    return "".join(f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n" for e in events)


def test_reasoning_deltas_are_not_dropped():
    up = Upstream((200, sse_events(
        {"choices": [{"index": 0, "delta": {"content": "", "reasoning": "thinking..."}}]},
        {"choices": [{"index": 0, "delta": {"content": '{"a": 1}'}, "finish_reason": "stop"}]},
        "[DONE]")))
    with client_for(up) as c:
        r = post(c, {"stream": True})
    assert "thinking..." in r.text


def test_streams_with_several_choices_stay_separate():
    up = Upstream((200, sse_events(
        {"choices": [{"index": 0, "delta": {"content": '{"a": '}}, {"index": 1, "delta": {"content": '{"b": '}}]},
        {"choices": [{"index": 0, "delta": {"content": "1,}"}}, {"index": 1, "delta": {"content": "2,}"}}]},
        "[DONE]")))
    with client_for(up) as c:
        r = post(c, {"stream": True}, **{"X-StreamFix-Stream-Mode": "json"})
    text = {0: "", 1: ""}
    for line in r.text.splitlines():
        if line.startswith("data: {"):
            for ch in json.loads(line[6:])["choices"]:
                text[ch["index"]] += ch["delta"].get("content") or ""
    assert json.loads(text[0]) == {"a": 1} and json.loads(text[1]) == {"b": 2}


def test_non_object_events_are_forwarded():
    up = Upstream((200, sse_events("123", {"choices": [{"index": 0, "delta": {"content": "{}"}}]}, "[DONE]")))
    with client_for(up) as c:
        r = post(c, {"stream": True})
    assert "data: 123" in r.text and r.text.rstrip().endswith("data: [DONE]")


class DroppingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield sse_events({"choices": [{"index": 0, "delta": {"content": '{"items": [1, 2'}}]}).encode()
        raise httpx.ReadError("connection reset")


def test_connection_drop_mid_stream_still_closes_the_json():
    def handler(request):
        return httpx.Response(200, stream=DroppingStream(), headers={"content-type": "text/event-stream"})
    with client_for(handler) as c:
        r = post(c, {"stream": True}, **{"X-StreamFix-Stream-Mode": "json"})
    assert json.loads(stream_text(r)) == {"items": [1, 2]}
    assert r.text.rstrip().endswith("data: [DONE]")


def test_usage_chunk_is_forwarded():
    up = Upstream((200, sse_events({"choices": [{"index": 0, "delta": {"content": "{}"}, "finish_reason": "stop"}]},
                                   {"choices": [], "usage": {"total_tokens": 7}}, "[DONE]")))
    with client_for(up) as c:
        r = post(c, {"stream": True})
    assert '"total_tokens": 7' in r.text


# --- publish review: hardening ---

@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"a": {"$ref": "http://169.254.169.254/latest/meta-data"}}},
    {"type": "string", "pattern": "^(a+)+$"},
    "not a schema",
])
def test_unsafe_or_bad_schemas_are_refused(schema):
    up = Upstream()
    with client_for(up) as c:
        assert post(c, {"schema": schema}).status_code == 400
    assert up.requests == []


@pytest.mark.parametrize("body", [{"model": {"x": 1}}, {"model": ""}, {"messages": "hi"}])
def test_malformed_requests_are_400(body):
    with client_for(Upstream()) as c:
        assert post(c, body).status_code == 400


def test_huge_content_is_passed_through_unrepaired():
    big = "{a: 1,}" + " " * 200_000
    with client_for(Upstream((200, completion(big)))) as c:
        r = post(c, {})
    assert r.headers["x-streamfix-status"] == "skipped"
    assert r.json()["choices"][0]["message"]["content"] == big


def test_head_and_no_api_docs():
    with client_for(Upstream()) as c:
        assert c.head("/").status_code == 200 and c.head("/health").status_code == 200
        assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
        assert "upstream" not in c.get("/health").json()
