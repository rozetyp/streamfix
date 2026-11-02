# StreamFix

[![ci](https://github.com/rozetyp/streamfix/actions/workflows/ci.yml/badge.svg)](https://github.com/rozetyp/streamfix/actions/workflows/ci.yml)

Get valid JSON out of LLM responses, including while they stream.

Models wrap JSON in markdown fences, think out loud in `<think>` blocks, leave trailing
commas, write `True`/`None`, and get cut off mid-object. StreamFix turns that into the
JSON value the model meant. It works on complete responses, on tool-call arguments, and
**live on a token stream**, so a UI rendering structured output never sees broken syntax.

~~~python
from streamfix import repair

repair("""<think>User wants {"name": ...} with an age</think>
Sure! Here it is:
```json
{name: 'Alice', age: 30, active: True, tags: ['a', 'b',],}
```""").value
# {'name': 'Alice', 'age': 30, 'active': True, 'tags': ['a', 'b']}
~~~

Syntax repair is done by [json-repair](https://github.com/mangiucugna/json_repair), a
widely used tolerant JSON parser. StreamFix adds what sits around it: reasoning- and
fence-aware extraction, picking the answer out of surrounding text, completion of
truncated literals, a character-level streaming repairer, schema contracts, and a proxy.
It passes all 213 of json-repair's own test cases and repairs 84 real broken responses
from 11 models; the numbers below are recomputed by the test suite on every run.

## Install

```sh
pip install "streamfix @ git+https://github.com/rozetyp/streamfix"          # library + CLI
pip install "streamfix[server] @ git+https://github.com/rozetyp/streamfix"  # + self-hosted proxy
```

## Library

```python
from streamfix import repair, repair_tool_arguments, StreamProcessor
from streamfix.contract import coerce_types, validate

result = repair(model_output)
result.status    # "pass" | "repaired" | "failed"
result.value     # parsed JSON value (None if failed)
result.repairs   # e.g. ["think_strip", "fence_strip", "remove_trailing_comma"]

# Tool calls: arguments are not guaranteed to be valid JSON
args, was_repaired = repair_tool_arguments(tool_call.function.arguments)

# Streaming: feed deltas, emit what comes back
proc = StreamProcessor(json_only=True)   # json_only drops prose around the JSON
for delta in stream:
    print(proc.feed(delta), end="")
print(proc.finish())                     # closes anything a cut-off stream left open
proc.result().value                      # authoritative full repair of the raw text

# Contracts: fix type drift, then check the schema
value, coercions = coerce_types(result.value, schema)   # "42" -> 42, "true" -> True
errors = validate(value, schema)
```

CLI: `streamfix repair response.txt --report` (or pipe the response on stdin).

## Self-hosted proxy

```sh
STREAMFIX_UPSTREAM_URL=https://openrouter.ai/api/v1 streamfix serve   # 127.0.0.1:8000
# or: docker build -t streamfix . && docker run -p 8000:8000 -e STREAMFIX_UPSTREAM_URL=... streamfix
```

It proxies `POST /v1/chat/completions`, so existing OpenAI client code keeps working with
a new `base_url`. Your key passes through to the upstream (or the server uses
`STREAMFIX_UPSTREAM_KEY`). There are no accounts and nothing is stored. `GET /` serves a
one-page overview of the project.

```python
client = OpenAI(base_url="http://localhost:8000/v1", api_key=OPENROUTER_KEY)
client.chat.completions.create(
    model="openai/gpt-4o-mini", messages=messages,
    extra_body={"schema": schema},                  # optional contract mode
    extra_headers={"X-StreamFix-Strict": "true"},   # optional
)
```

| Per request | Effect |
|---|---|
| `"schema": {...}` in the body | Contract mode: coerce types, validate, one corrective retry. Local `$ref`s only, no regex keywords. |
| `X-StreamFix-Strict: true` | 422 instead of best effort when no JSON is recoverable |
| `X-StreamFix-Stream-Mode: json` | when streaming, send only the JSON value |
| `X-StreamFix-Repair: off` | leave the response untouched |

| Server environment | Default |
|---|---|
| `STREAMFIX_UPSTREAM_URL` (or `--upstream`) | `https://api.openai.com/v1` |
| `STREAMFIX_UPSTREAM_KEY` | none: callers must send their own key |
| `STREAMFIX_FALLBACK_MODELS` | none; comma-separated models to try when the upstream errors |
| `STREAMFIX_MAX_BODY_BYTES` | 1000000 |

Non-streamed responses carry `X-StreamFix-Status` (`pass`, `repaired`, `failed`, or
`skipped` for content over 100,000 characters), `X-StreamFix-Applied`,
`X-StreamFix-Tool-Args-Repaired` and `X-StreamFix-Model`; in contract mode also
`X-StreamFix-Schema-Valid`, `X-StreamFix-Schema-Errors` and `X-StreamFix-Retry-Count`.
Streamed responses carry `X-StreamFix-Model` and `X-StreamFix-Stream-Mode`.

A demo instance runs at `https://streamfix.dev/v1`, forwarding to OpenRouter. It has no key
of its own, so send your OpenRouter key. Requests pass through it and are not stored, but
if you'd rather not route them through a third party, run your own; it's one command.

## How it works

```
response ──► preprocess ──► pick the answer ──► partial literals ──► json-repair ──► value
             strip <think>,   one JSON span, or    {"ok": tru → true   tolerant
             keep fenced      the whole text       {"k":      → null   parser
             JSON (chunk-safe)
```

**Preprocessing** is a streaming-safe scanner. `<think>` tags and fences can arrive split
across chunks (`<th` + `ink>`), so it carries a short tail between chunks; the tests check
it gives the same result at chunk sizes 1, 2, 3, 8, and 64. It knows when it is inside a
JSON string, so `{"md": "wrap code in ``` fences"}` and `{"html": "<think>"}` stay intact.
Reasoning sent with only a closing `</think>` (as Kimi does) is dropped too.

**Picking the answer.** When JSON values are separated by prose, one of them is the answer:
the opening one if the response starts with JSON ("`{...}` Note: replace `{name}`..."),
otherwise the last valid one ("Original: `{...}` Corrected: `{...}`"), or a value cut off
at the end of the output. Values glued together (`{...}, {...}`) follow json-repair's
conventions.

**Streaming repair** (`StreamRepairer`) rewrites characters as they arrive and holds back
only what it must: a comma until the next token shows whether it was trailing, and a bare
word until it ends (`Tr` + `ue` → `true`, `in prog` + `ress` → `"in progress"`). It inserts
missing commas, quotes stray text, rewrites mismatched closers to match what is open, and
at the end of a cut-off stream closes strings, gives a dangling key `null`, and closes
brackets. Fuzzing it with 20,000 randomly broken documents produced no invalid output, and
valid JSON passes through unchanged.

## Measured

Computed by `tests/test_readme.py` on every test run, so this table can't go stale:

| | json-repair | StreamFix |
|---|---|---|
| json-repair's own test suite (213 cases) | 213 | **213** |
| StreamFix's cases (61) | 54 | **61** |
| StreamFix's cases, streamed one character at a time (60) | n/a | **60** |

Read the second row with care: those cases were written knowing where json-repair falls
short (reasoning blocks that contain JSON-like text, partial `tru`/`nul`, a dangling
`"key":`). Streaming has no json-repair column because json-repair parses complete text.

The tests also replay 84 real responses, from 11 models, that broke `json.loads` in earlier
benchmarks (`tests/data/real_outputs.json`). All of them repair, including Kimi's
reasoning-before-`</think>` drafts and a Llama tool call that switched to curly quotes
halfway through.

## Limitations

- Repair recovers what the model meant to write; it cannot recover what it never wrote.
  A truncated response comes back valid but incomplete.
- Picking one answer out of several JSON values is a heuristic. Glued-together values
  follow json-repair's convention (often a list).
- A stream can't take back what it already sent: a draft JSON before an unannounced
  `</think>` reaches the client. `proc.result()` (full repair of the raw text) is the
  authoritative answer when the two differ.
- Contract mode retries once and only for non-streaming requests.
- The proxy binds to `127.0.0.1` by default and has no auth of its own. Put it behind
  your own network controls, and rate limiting, before exposing it.

## Development

```sh
pip install -e '.[dev]' && pytest
```

## Background

StreamFix started in December 2025 as a hosted, credit-billed proxy. The engineering held
up; the business didn't, because provider-native structured outputs keep improving and
json-repair is free and in-process. Its own regex-based repair engine passed 75 of
json-repair's 213 cases, which is why syntax repair is now delegated. Version 2 keeps what
was genuinely useful (reasoning-aware extraction, streaming repair, contracts) and
open-sources it.

## License

MIT. The cases in `tests/data/json_repair_cases.json` come from json-repair (MIT, see
`tests/data/JSON_REPAIR_LICENSE`).
