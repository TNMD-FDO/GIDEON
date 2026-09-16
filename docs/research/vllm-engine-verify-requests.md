---
verified_against:
  - pin: images.vllm-openai
    version: v0.27.1
  - pin: models.generator
    version: 017b9c7af6b5689d5dd426a76e0bc077eb5ca20a
---
# `gideon engine verify` — vLLM v0.27.1 request/response contract

Scope: GIDEON's `gideon engine verify` (host command; sends Chat Completions
requests from inside `gideon-generator` and grades replies). Every claim is
checked against `vllm-project/vllm` tag `v0.27.1`
(`6e448d0ea9bf3d88d898b65449ca6dc2aec170ac`), cloned fresh
(`git clone --depth 1 --branch v0.27.1`), and against
`Qwen/Qwen3.8-27B-FP8` at revision `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`
on the Hugging Face Hub (fetched directly — the repo and pinned commit both
exist; `sha` in the Hub API response equals the pinned revision). File paths
are relative to the vLLM repo root at the tag. This note assumes
`docs/research/vllm-engine-service.md` (already on `main`); it does not
repeat that note's findings.

## 1. `POST /tokenize`

Two request shapes, a discriminated union `TokenizeRequest =
TokenizeCompletionRequest | TokenizeChatRequest`
(`vllm/entrypoints/serve/tokenize/protocol.py:24-159`):

- **Completion form**: `model: str|None`, `prompt: str` (required),
  `add_special_tokens: bool = True`, `return_token_strs: bool = False`.
- **Chat form**: `model`, `messages` (required), `add_generation_prompt: bool
  = True`, `continue_final_message: bool = False` (mutually exclusive with
  `add_generation_prompt`, enforced by a `model_validator`),
  `add_special_tokens: bool = False`, `chat_template`, `chat_template_kwargs`,
  `tools`, etc.

Response (`TokenizeResponse`, same file, line 159): `count: int`,
`max_model_len: int`, `tokens: list[int]`, `token_strs: list[str]|None`.

**Auth bypass — confirmed.** `attach_router` in
`vllm/entrypoints/serve/tokenize/api_router.py:94-109` calls
`app.include_router(router)` with **no prefix**, and the router's path is the
bare string `"/tokenize"` (line 37). `GUARDED_PREFIX = ("/v1", "/v2",
"/inference", "/cohere")` in `vllm/entrypoints/serve/utils/server_utils.py:42`
is checked with `url_path.startswith(GUARDED_PREFIX)`; `"/tokenize"` matches
none of the four, so the middleware never runs — `/tokenize` is
unauthenticated at v0.27.1 even when `VLLM_API_KEY`/`--api-key` is set. This
matches `vllm-engine-service.md`'s claim.

**Does `messages` apply the chat template with a count matching a Chat
Completions request?** Yes, for matching defaults. `create_tokenize`
(`serving.py:57-124`) calls `self.online_renderer.preprocess_chat(...)` for
the chat form — the same renderer path a Chat Completions request uses.
Both `TokenizeChatRequest` (line 54) and `ChatCompletionRequest`
(`vllm/entrypoints/openai/chat_completion/protocol.py:310-315`) default
`add_generation_prompt=True`; both default `add_special_tokens=False`
(`protocol.py:328`). So with no `chat_template_kwargs`/`reasoning_effort`
override on either side, `/tokenize`'s `count` for the `messages` form equals
the prompt-token count a Chat Completions request with the same messages
would consume. **Caveat**: `ChatCompletionRequest.build_chat_params`
(`protocol.py:555-589`) injects `enable_thinking` into
`chat_template_kwargs` automatically when `reasoning_effort` is set
(`!= None`); `TokenizeChatRequest.build_chat_params` does not do this
substitution — so a verify probe that sets `reasoning_effort` on the Chat
Completions call must mirror the derived `chat_template_kwargs` on the
`/tokenize` call by hand to keep counts comparable.

## 2. Structured outputs

**Request shapes.** `ChatCompletionRequest` has both `response_format:
AnyResponseFormat|None` (line 229, OpenAI's `{"type": "json_schema",
"json_schema": {"name","schema","strict"?}}` shape, via `ResponseFormat`/
`JsonSchemaResponseFormat`) and `structured_outputs:
StructuredOutputsParams|None` (line 375, vLLM's own `extra_body` field:
`json`, `regex`, `choice`, `grammar`, `json_object`, `structural_tag`,
`disable_any_whitespace`, `disable_additional_properties`,
`whitespace_pattern` — `vllm/sampling_params.py:72-84`).
`extract_structured_outputs()` (`protocol.py:639-644`) merges
`response_format` into `structured_outputs` via
`structured_outputs_from_response_format`
(`vllm/entrypoints/openai/engine/protocol.py:179-206`), mapping
`json_schema` → `structured_outputs.json`. **Correction to the ticket's
premise**: there is no legacy top-level `guided_json` field on
`ChatCompletionRequest` at v0.27.1 — only `structured_outputs` (new-style)
exists; `guided_decoding_backend` survives only on the legacy `CompletionRequest`
(line 1099).

**Backend resolution.** `StructuredOutputsConfig.backend` (`vllm/config/
structured_outputs.py:19`) defaults `"auto"`. Resolution happens in
`SamplingParams._validate_structured_outputs`/`verify`
(`vllm/sampling_params.py:923-1080`): with `backend == "auto"`, vLLM first
tries `validate_xgrammar_grammar(self)`; **if that raises**, it falls back to
`guidance`, or to `outlines` if the schema has guidance-unsupported features
(`sampling_params.py:1042-1077`) — **`auto` never hard-fails on an
xgrammar-unsupported JSON Schema; it silently switches backend.** A
schema-rejection HTTP error therefore only occurs when a *specific* backend
(e.g. `--structured-outputs-config backend=xgrammar`) is forced.

**xgrammar's pinned version is a range, not an exact pin**:
`requirements/common.txt:27` says `xgrammar >= 0.2.1, < 1.0.0` — the exact
wheel resolved into the image could not be confirmed without pulling it.

**Keywords vLLM itself rejects for xgrammar** (`has_xgrammar_unsupported_json_features`,
`vllm/v1/structured_output/backend_xgrammar.py:225-267`): numeric
`multipleOf`; array `uniqueItems`/`contains`/`minContains`/`maxContains`;
string `format` values outside a fixed allow-list of 14 (`email`, `date`,
`time`, `date-time`, `duration`, `ipv4`, `ipv6`, `hostname`, `uuid`, `uri`,
`uri-reference`, `uri-template`, `json-pointer`, `relative-json-pointer` —
lines 207-222); object `patternProperties`/`propertyNames`. Everything else
(`type`, `properties`, `required`, `additionalProperties`, `enum`, `const`,
`items`, `minItems`/`maxItems`, `minimum`/`maximum`, `minLength`/`maxLength`,
`pattern`, `$ref`/`$defs`, `anyOf`/`oneOf`) passes vLLM's own guard and is
forwarded to `xgr.Grammar.from_json_schema(schema)`; a further rejection at
that layer (xgrammar's own compiler) also becomes a `ValueError`. A fuller
xgrammar-keyword table beyond vLLM's own blocklist exists (xgrammar's
DeepWiki page, a third-party test-suite blog) but is secondary and not
independently re-verified against the pinned wheel — flagged below.

**Reasoning gate — confirmed in code.** `StructuredOutputManager`
(`vllm/v1/structured_output/__init__.py`) only advances/fills the grammar
bitmask once reasoning has ended: `should_fill_bitmask`/`should_advance`
(lines 362-410) check `request.structured_output_request.reasoning_ended`,
set via `reasoner.is_reasoning_end(request.prompt_token_ids)` (initial) or
`is_reasoning_end_streaming` (mid-generation) — gated off entirely if
`enable_in_reasoning` (default `False`, `structured_outputs.py:41`). So yes:
the grammar applies only after the model's `</think>`, unless
`enable_in_reasoning=True`. `--reasoning-parser qwen3` wires this
automatically: `EngineArgs.create_engine_config`
(`vllm/engine/arg_utils.py:2415-2416`) copies `self.reasoning_parser` into
`structured_outputs_config.reasoning_parser` — no separate
`--structured-outputs-config reasoning_parser=...` flag is needed.

**Error shape for an unsupported schema keyword** (forced, non-`auto`
backend): `has_xgrammar_unsupported_json_features`/`from_json_schema`
raise a plain `ValueError`. `create_error_response`
(`vllm/entrypoints/serve/utils/error_response.py:64-66`) maps bare
`ValueError`/`TypeError`/`OverflowError` to **HTTP 400**, `err_type =
"BadRequestError"`. Body shape (`ErrorResponse`/`ErrorInfo`,
`vllm/entrypoints/openai/engine/protocol.py:63-71`):
`{"error": {"message": "...", "type": "BadRequestError", "param": null,
"code": 400}}`.

## 3. `chat_template_kwargs` and the pinned Qwen template

`chat_template_kwargs: dict[str, Any] | None` on `ChatCompletionRequest`
(`protocol.py:357-362`); merged with `add_generation_prompt`,
`continue_final_message`, `documents`, `reasoning_effort`, and a derived
`enable_thinking` (`protocol.py:560-582`).

At the pinned revision, `chat_template.jinja` (fetched directly from
`huggingface.co/Qwen/Qwen3.8-27B-FP8/raw/017b9c7af6b5689d5dd426a76e0bc077eb5ca20a/chat_template.jinja`):
with `add_generation_prompt` true and **`enable_thinking: false`**, the
template emits (line ~165) `<|im_start|>assistant\n<think>\n\n</think>\n\n`
— an already-closed, empty think block baked into the *prompt*. With
`enable_thinking` unset/true, it emits only `<think>\n` (open). This matters
because vLLM's qwen3 reasoning parser (`rust/src/parser/src/reasoning/
qwen3.rs`, backed by `DelimitedReasoningParser`,
`rust/src/parser/src/reasoning/delimited.rs:64-72`) seeds its
`current_in_reasoning` state from the **last reasoning boundary found in
the prompt tokens**: with the empty pre-closed block, the last boundary is
`</think>`, so `current_in_reasoning` starts `false` and the entire
generated response is parsed as `content` — no `reasoning` field is ever
populated. With the open `<think>` prompt (default), `current_in_reasoning`
starts `true` and the model's own `</think>` closes it mid-stream.

**`reasoning_effort` is honored by this template directly** — a chat-template
mechanism, not a vLLM one. Lines ~44-51 of the template: if thinking is
enabled, `resolved_reasoning_effort = reasoning_effort|default('xhigh')`,
and it **must be one of `xhigh`, `medium`, `low`** or the template calls
`raise_exception(...)`. vLLM's own `reasoning_effort` field accepts a wider
OpenAI-shaped set — `none, minimal, low, medium, high, xhigh, max`
(`protocol.py:244-253`) — and only special-cases `"none"` (forces
`enable_thinking=False`, bypassing the template's raise). **Passing
`reasoning_effort: "high"`, `"minimal"`, or `"max"` on this model raises a
Jinja `TemplateError`, mapped to HTTP 400** (`error_response.py`'s
`TemplateError` branch, line ~73). This is a concrete gotcha for
`engine verify` probes that sweep `reasoning_effort` values.

## 4. Usage accounting

**Streaming** (`vllm/entrypoints/openai/chat_completion/serving.py`):
`should_include_usage` (`vllm/entrypoints/serve/utils/api_utils.py:276-288`)
turns on `include_usage` only from `stream_options.include_usage` (or
`--enable-force-include-usage`, off by default). The final usage chunk
(lines 786-807) is `ChatCompletionStreamResponse(..., choices=[],
usage=UsageInfo(prompt_tokens, completion_tokens, total_tokens))`, and it is
yielded **before** `data: [DONE]\n\n` (usage chunk at line 807, `[DONE]` at
line 842 — confirmed by line order in the same generator). Non-streaming
usage is built the same way at lines 1053-1069.

`num_prompt_tokens = len(final_res.prompt_token_ids)` (line 1054 /
`res.prompt_token_ids` at 488) — this is the engine's fully-tokenized
post-chat-template prompt (system + user + template scaffolding), not raw
text length. `completion_tokens` sums `len(output.token_ids)` across all
generated tokens (lines 632, 1057-1058) with **no distinction between
reasoning and non-reasoning tokens** — reasoning tokens are included.
`UsageInfo` (`vllm/entrypoints/openai/engine/protocol.py:115-119`) has only
`prompt_tokens`, `total_tokens`, `completion_tokens`, and
`prompt_tokens_details` (cached/multimodal breakdown) — **there is no
`completion_tokens_details.reasoning_tokens` field at v0.27.1.**

## 5. Sampling overrides

`--generation-config auto` (default, `vllm/config/model.py:312`) makes
`get_diff_sampling_param` (`model.py:1593-1646`) pull
`repetition_penalty`, `temperature`, `top_k`, `top_p`, `min_p`, and
`max_new_tokens` (renamed `max_tokens`) from the model's own
`generation_config.json`, applied as `default_sampling_params`. In
`ChatCompletionRequest.to_sampling_params` (`protocol.py:646-690`), each of
`temperature`/`top_p`/`top_k`/`min_p`/`repetition_penalty` uses the
per-request value **only if it is not `None`**, else falls back to
`default_sampling_params.get(field, engine_hardcoded_default)`. `seed` is
passed straight through (`seed=self.seed`, no generation_config participation
— HF `generation_config.json` has no `seed` concept). `temperature: 0`
(`< _SAMPLING_EPS = 1e-5`, `sampling_params.py:27,718-720`) sets
`sampling_type = SamplingType.GREEDY`.

## 6. Window overflow

The check lives in `vllm/renderers/params.py` (`TokenizeParams`, used by
both `/tokenize` and Chat Completions' pre-generation length check).
`max_input_tokens = max_total_tokens - max_output_tokens` (property, lines
204-210); `_token_len_check` (lines 438-459) raises `VLLMValidationError`
(→ HTTP 400, `BadRequestError`, `param: "input_tokens"`) when
`len(prompt_tokens) > max_input_tokens`, i.e. **the check is
`prompt_tokens + max_output_tokens > max_model_len`**, not `prompt_tokens >=
max_model_len` alone. For Chat Completions, `build_tok_params`
(`chat_completion/protocol.py:592-609`) sets `max_total_tokens =
model_config.max_model_len` and `max_output_tokens = (max_completion_tokens
or max_tokens) or 0` — **if the client omits `max_tokens` entirely, the
check degenerates to `prompt_tokens > max_model_len`** (effective budget
zero). Message text (verbatim, `params.py:448-456`): `"This model's maximum
context length is {max_total_tokens} tokens. However, you requested
{max_output_tokens} output tokens and your prompt contains
{qualifier}{token_count} input tokens, for a total of
{qualifier}{total} tokens. Please reduce the length of the input prompt or
the number of requested output tokens."` `max_tokens` is **refused**
(request rejected), never silently clipped.

## 7. The image's shell and curl

Base image confirmed via `docker/Dockerfile`: `nvidia/cuda:13.0.3-base-
ubuntu22.04` (`docker/Dockerfile:25-46`, matches `vllm-engine-service.md`
§3). `curl` is installed in the shared `vllm-base` stage
(`docker/Dockerfile:673-678`) that `vllm-openai` is built `FROM`, so it is
present; `/bin/sh` is Ubuntu's default and nothing in the Dockerfile removes
it. **No `HEALTHCHECK` instruction exists anywhere in `docker/Dockerfile`**
at this tag (`grep -n "^HEALTHCHECK"` — no match) — correcting the premise
that "the image's healthcheck already uses curl"; curl's presence is
established independently, not via a built-in healthcheck.

Ubuntu 22.04 (jammy)'s `curl` package is `7.81.0` (security-patched
revisions like `7.81.0-1ubuntu1.24`+, per packages.ubuntu.com — **secondary
source**; the exact patch level in the published image was not
pulled/inspected). curl's manual (curl.se manpage, cross-checked against a
local curl 8.18.0 install whose `-w`/`-K` semantics are unchanged) confirms:
`-K/--config` reads one option per line, and a header line reads
`header = "Name: value"` (quotes required for `:`/`=`/whitespace);
`--data-binary @-` reads the POST body from stdin verbatim; `-w` variables
`http_code`, `time_starttransfer`, and `time_total` are documented.

**`/tmp` writability**: the Dockerfile sets no `USER` for the default
`vllm-openai` target (root, per `vllm-engine-service.md` §3) and declares no
read-only filesystem anywhere — that is strictly a `docker run`/compose-time
flag (`--read-only`), never image-baked. So `/tmp` is writable by default
unless GIDEON's own compose file opts into a read-only root.

## 8. Reasoning delta field name

Confirmed at the tag: both `ChatMessage` (full response,
`chat_completion/protocol.py:68-71`) and `DeltaMessage` (streamed,
`vllm/entrypoints/openai/engine/protocol.py:392-397`) carry only
**`reasoning: str | None`** — there is no `reasoning_content` field in any
response. `reasoning_content` exists only as a **deprecated input alias** on
request *messages* (assistant-turn history), silently renamed to
`reasoning` by a `model_validator` (`chat_completion/protocol.py:507-531`)
before validation. This confirms `owui-engine-connection.md`'s observed
`delta.reasoning`, and rules out `reasoning_content` ever appearing on the
wire from the engine at v0.27.1.

## Implications for `engine verify`

- `/tokenize` succeeds without a bearer token (a valid assertion in itself);
  keep the token on Chat Completions calls. Mirror any `reasoning_effort`
  → `enable_thinking` derivation onto `/tokenize`'s `chat_template_kwargs`
  if comparing counts.
- Avoid `reasoning_effort` values other than `none`/`medium`/`low`/`xhigh`
  against this model — `high`/`minimal`/`max` 400 with a template error
  (assertable, but don't expect success from them).
- A structured-output success probe should stick to vLLM's
  confirmed-safe keyword set (§2); a rejection probe (`multipleOf`,
  `uniqueItems`, an unsupported `format`, `patternProperties`) only 400s
  when a specific backend is forced — `auto` (GIDEON's default) silently
  falls back instead.
- Assert reasoning/content splitting with one probe at default
  `enable_thinking` (expect populated `delta.reasoning`) and one with
  `chat_template_kwargs: {"enable_thinking": false}` (expect none).
- For overflow, omit `max_tokens` to hit the `prompt_tokens > max_model_len`
  case, and set an explicit large `max_tokens` for the additive case; expect
  HTTP 400 `BadRequestError` with §6's message template.
- With `stream_options: {"include_usage": true}`, assert the usage chunk's
  `choices == []` arrives strictly before `data: [DONE]`.

## Could not verify

- The exact resolved `xgrammar` wheel version inside the published
  `vllm/vllm-openai:v0.27.1` image (the requirement is a range,
  `>=0.2.1,<1.0.0`; the image itself was not pulled/inspected).
- The complete xgrammar JSON-Schema-keyword support matrix beyond vLLM's own
  pre-check blocklist (§2) — the fuller table cited is from xgrammar's
  DeepWiki page and a third-party comparison blog, not xgrammar's own docs
  site content fetched directly.
- The exact Ubuntu `curl` patch revision baked into the published
  `v0.27.1` image (Ubuntu package-archive lookup used instead of pulling the
  image).
- Whether GIDEON's own compose file for `gideon-generator` sets
  `read_only: true` (a GIDEON-side config question, out of scope for the
  upstream source this note covers).
