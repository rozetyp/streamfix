"""Self-hosted proxy for OpenAI-style chat completions that repairs JSON in responses.

Point an OpenAI SDK at it (``base_url="http://localhost:8000/v1"``); only
``POST /v1/chat/completions`` is proxied. Requests go to the upstream with the
caller's own Authorization header (or the server's configured key), and responses
come back with the JSON repaired.

Per-request controls (headers):
  X-StreamFix-Repair: off        leave content untouched
  X-StreamFix-Strict: true       422 instead of best effort when no JSON is recoverable
  X-StreamFix-Stream-Mode: json  when streaming, drop text outside the JSON value

Contract Mode: add a non-standard ``"schema": {...}`` field to the request body
(``extra_body=`` in the OpenAI SDK). The repaired JSON is type-coerced and validated,
with one corrective retry. Only local ``$ref``s, and no regex keywords, are accepted.

Configuration (environment):
  STREAMFIX_UPSTREAM_URL     default https://api.openai.com/v1
  STREAMFIX_UPSTREAM_KEY     used when the caller sends no Authorization header
  STREAMFIX_FALLBACK_MODELS  comma-separated models to try if the requested one fails
  STREAMFIX_MAX_BODY_BYTES   largest accepted request body (default 1 MB)

Responses larger than MAX_REPAIR_CHARS are passed through unrepaired (status
"skipped"); repair runs in a worker thread so one slow response can't stall others.
GET / serves the project page (index.html, shipped inside the package).
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .contract import check_schema, coerce_types, retry_prompt, validate
from .repair import repair, repair_tool_arguments
from .stream import StreamProcessor

MAX_CONTRACT_ATTEMPTS = 2
MAX_REPAIR_CHARS = 100_000  # json-repair can be slow on huge, adversarial input
RETRYABLE = {408, 429} | set(range(500, 600))  # worth trying the next fallback model


def create_app(
    upstream_url: str | None = None,
    upstream_key: str | None = None,
    fallback_models: list[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    upstream_url = (upstream_url or os.getenv("STREAMFIX_UPSTREAM_URL")
                    or "https://api.openai.com/v1").rstrip("/")
    upstream_key = upstream_key or os.getenv("STREAMFIX_UPSTREAM_KEY")
    if fallback_models is None:
        fallback_models = [m.strip() for m in os.getenv("STREAMFIX_FALLBACK_MODELS", "").split(",")
                           if m.strip()]
    max_body = int(os.getenv("STREAMFIX_MAX_BODY_BYTES", 1_000_000))
    page = files("streamfix").joinpath("index.html").read_text(encoding="utf-8")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = httpx.AsyncClient(transport=transport,
                                             timeout=httpx.Timeout(120.0, connect=10.0))
        yield
        await app.state.client.aclose()

    app = FastAPI(title="StreamFix", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def upstream_headers(request: Request) -> dict[str, str]:
        auth = request.headers.get("authorization")
        if not auth and upstream_key:
            auth = f"Bearer {upstream_key}"
        if not auth:
            raise HTTPException(401, "No Authorization header and no STREAMFIX_UPSTREAM_KEY configured")
        return {"Authorization": auth, "Content-Type": "application/json"}

    async def send(body: dict, headers: dict, stream: bool) -> tuple[httpx.Response, str]:
        """POST to upstream, trying fallback models in order. Returns (response, model)."""
        client: httpx.AsyncClient = app.state.client
        url = f"{upstream_url}/chat/completions"
        models = [m for m in dict.fromkeys([body.get("model"), *fallback_models]) if m]
        if not models:
            raise HTTPException(400, "Request body needs a model")
        status, detail = 502, ""
        for model in models:
            req = client.build_request("POST", url, json={**body, "model": model}, headers=headers)
            try:
                resp = await client.send(req, stream=stream)
            except httpx.HTTPError as e:
                status, detail = 502, f"upstream unreachable: {type(e).__name__}: {e}"
                continue
            if resp.status_code == 200:
                return resp, model
            status = resp.status_code
            detail = (await resp.aread()).decode(errors="replace")[:500]
            await resp.aclose()
            if status not in RETRYABLE:
                break  # a bad key or bad request fails the same way on every model
        raise HTTPException(status, detail)

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def home() -> str:
        return page

    @app.api_route("/health", methods=["GET", "HEAD"])
    async def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_body:
            raise HTTPException(413, f"Request body over {max_body} bytes")
        raw = await request.body()
        if len(raw) > max_body:
            raise HTTPException(413, f"Request body over {max_body} bytes")
        try:
            body: dict[str, Any] = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "Request body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be a JSON object")
        if not isinstance(body.get("model"), str) or not body["model"].strip():
            raise HTTPException(400, '"model" must be a non-empty string')
        if not isinstance(body.get("messages"), list):
            raise HTTPException(400, '"messages" must be a list')
        schema = body.pop("schema", None)
        if schema is not None:
            problem = check_schema(schema, allow_patterns=False)
            if problem:
                raise HTTPException(400, f"Invalid JSON schema: {problem}")
        repair_on = request.headers.get("x-streamfix-repair", "on").lower() != "off"
        if not repair_on:
            schema = None  # contract mode needs repair; don't claim a validation we skipped
        strict = request.headers.get("x-streamfix-strict", "").lower() == "true"
        headers = upstream_headers(request)

        if body.get("stream"):
            if strict or schema is not None:
                raise HTTPException(400, "Strict mode and schema validation need stream=false")
            json_only = request.headers.get("x-streamfix-stream-mode", "").lower() == "json"
            resp, model = await send(body, headers, stream=True)
            return StreamingResponse(
                _stream(resp, json_only if repair_on else None),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", **_model_header(model),
                         "X-StreamFix-Stream-Mode": "json" if json_only else "passthrough"},
            )

        messages = list(body.get("messages", []))
        attempts = 0
        while True:
            attempts += 1
            resp, model = await send({**body, "messages": messages}, headers, stream=False)
            try:
                data = resp.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                raise HTTPException(502, "Upstream returned a non-JSON response")
            out = await run_in_threadpool(_repair_completion, data, schema, repair_on)
            if schema is None or not out["schema_errors"] or attempts >= MAX_CONTRACT_ATTEMPTS:
                break
            messages += [{"role": "assistant", "content": out["original"] or ""},
                         {"role": "user", "content": retry_prompt(out["schema_errors"], schema)}]

        hdrs = {
            "X-StreamFix-Status": out["status"],
            "X-StreamFix-Applied": ",".join(out["repairs"]) or "none",
            "X-StreamFix-Tool-Args-Repaired": str(out["tool_args_repaired"]),
            **_model_header(model),
        }
        if schema is not None:
            hdrs["X-StreamFix-Schema-Valid"] = "false" if out["schema_errors"] else "true"
            hdrs["X-StreamFix-Schema-Errors"] = str(len(out["schema_errors"]))
            hdrs["X-StreamFix-Retry-Count"] = str(attempts - 1)
        if strict and out["status"] == "failed":
            return JSONResponse(status_code=422, headers=hdrs, content={"error": {
                "type": "json_unrecoverable",
                "message": "No JSON could be extracted or repaired from the model output",
                "content_preview": (out["original"] or "")[:500],
                "schema_errors": out["schema_errors"][:5],
            }})
        return JSONResponse(content=data, headers=hdrs)

    return app


def _model_header(model: str) -> dict[str, str]:
    # Header values must be Latin-1; skip the header for model names that aren't.
    return {"X-StreamFix-Model": model} if model.isascii() else {}


def _repair_completion(data: dict, schema: dict | None, repair_on: bool) -> dict:
    """Repair every choice of a chat completion in place; report on the first.

    Contract mode (schema) applies to the first choice, which is what retries correct.
    """
    choices = [c for c in data.get("choices") or [] if isinstance(c, dict)]
    first = (choices[0].get("message") or {}) if choices else {}
    report = {"original": first.get("content"), "status": "pass", "repairs": [],
              "tool_args_repaired": 0, "schema_errors": []}
    if not repair_on:
        return report

    for i, choice in enumerate(choices):
        message = choice.get("message") or {}
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            if isinstance(fn.get("arguments"), str) and 0 < len(fn["arguments"]) <= MAX_REPAIR_CHARS:
                fn["arguments"], fixed = repair_tool_arguments(fn["arguments"])
                report["tool_args_repaired"] += fixed

        content = message.get("content")
        if not isinstance(content, str) or not content:
            continue  # absent, or multi-part content we don't rewrite
        if len(content) > MAX_REPAIR_CHARS:
            if i == 0:
                report["status"] = "skipped"
            continue
        result = repair(content)
        if not result.ok:
            if i == 0:
                report["status"] = "failed"
                if schema is not None:
                    report["schema_errors"] = ["no JSON found in the response"]
            continue
        value, repairs = result.value, list(result.repairs)
        if schema is not None and i == 0:
            value, coercions = coerce_types(value, schema)
            if coercions:
                repairs.append("type_coerce")
            report["schema_errors"] = validate(value, schema)
        if repairs:
            message["content"] = json.dumps(value, ensure_ascii=False)
            if i == 0:
                report["status"] = "repaired"
        if i == 0:
            report["repairs"] = repairs

    if report["tool_args_repaired"] and report["status"] == "pass":
        report["status"] = "repaired"
    return report


def _tail_chunk(tail: str, index: int, like: dict | None) -> str:
    like = like or {}
    chunk = {"id": like.get("id", "streamfix"), "object": "chat.completion.chunk",
             "created": like.get("created", 0), "model": like.get("model", ""),
             "choices": [{"index": index, "delta": {"content": tail}, "finish_reason": None}]}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def _stream(resp: httpx.Response, json_only: bool | None) -> AsyncIterator[str]:
    """Re-emit upstream SSE with content deltas repaired. json_only=None disables repair.

    Each choice (n > 1) gets its own processor. Before a choice's finish_reason chunk,
    its held and closing text is sent, because clients may stop reading there.
    """
    procs: dict[int, StreamProcessor] = {}
    last: dict | None = None
    try:
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload) if json_only is not None else None
            except ValueError:
                chunk = None
            if not isinstance(chunk, dict) or not isinstance(chunk.get("choices"), list):
                yield f"data: {payload}\n\n"
                continue
            last = chunk
            tails, kept = [], []
            for choice in chunk["choices"]:
                index = choice.get("index", 0)
                proc = procs.setdefault(index, StreamProcessor(json_only=json_only))
                delta = choice.get("delta") or {}
                if isinstance(delta.get("content"), str):
                    delta["content"] = proc.feed(delta["content"])
                    if not delta["content"] and set(delta) <= {"content"} \
                            and not choice.get("finish_reason"):
                        continue  # nothing to show yet; reasoning or role deltas still pass
                if choice.get("finish_reason"):
                    tail = proc.finish()
                    if tail:
                        tails.append(_tail_chunk(tail, index, chunk))
                kept.append(choice)
            for t in tails:
                yield t
            if kept or not chunk["choices"]:  # usage-only chunks have no choices
                chunk["choices"] = kept
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    except httpx.HTTPError:
        yield ": upstream connection lost\n\n"
    finally:
        await resp.aclose()
    for index, proc in procs.items():
        tail = proc.finish()
        if tail:
            yield _tail_chunk(tail, index, last)
    yield "data: [DONE]\n\n"


app = create_app()
