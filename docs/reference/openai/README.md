# OpenAI API Specification — North Star reference

> Strict OpenAI `/v1/chat/completions` compatibility is AuditTrace-AI's
> biggest integration asset. This folder is the canonical upstream
> reference we align against. **Nothing in this folder is our code** —
> it is vendored directly from OpenAI so we never have to guess at
> "what OpenAI says" during a design discussion.

## The rule

**Every response shape AuditTrace-AI emits on an OpenAI-compatible
path (`/v1/chat/completions`, `/v1/models`, etc.) must be a strict
SUPERSET of the schemas defined in `openapi.yaml`.**

- OpenAI-required keys are always present, with OpenAI-compatible
  semantics.
- AuditTrace-specific extensions (`status`, `operator_hint`,
  `trace_id`, `user_facing_message`, `X-Project`, `X-Memory-Mode`,
  `X-Thinking`, …) are additive — net-new keys or net-new headers
  — never replacements of OpenAI fields, never required for the
  default path to work.
- A client that only understands OpenAI keys must keep working
  unchanged when pointed at AuditTrace-AI.

See `~/.claude/projects/.../memory/feedback_openai_schema_inviolate.md`
for the full principle and the ADR-024 regression precedent.

## Source

| File | Origin | Pulled on |
|---|---|---|
| `openapi.yaml` | <https://app.stainless.com/api/spec/documented/openai/openapi.documented.yml> | 2026-04-16 |

The Stainless-hosted spec is the version OpenAI themselves point to
from `github.com/openai/openai-openapi/blob/master/README.md` as
"The most recent OpenAPI specification for the OpenAI API".

An alternate human-curated version lives at
<https://github.com/openai/openai-openapi/tree/manual_spec> — cleaner
but sometimes lags the Stainless auto-generated one.

## Refresh

```bash
./scripts/refresh-openai-spec.sh
```

Re-pulls the Stainless YAML and updates the "Pulled on" date in this
README. Run periodically (~monthly) or when OpenAI announces a spec
change. Commit the refreshed file so diffs are reviewable.

## The three contracts AuditTrace must honour

### 1. `CreateChatCompletionResponse` (non-streaming success)

Canonical shape (see `openapi.yaml` line ~37273):

```yaml
id:            string            # required
object:        "chat.completion" # required
created:       integer           # required, unix seconds
model:         string            # required
choices:                         # required, array
  - index:         integer
    message:
      role:    "assistant"
      content: string | null
      tool_calls: array          # optional
    finish_reason: "stop" | "length" | "tool_calls" | "content_filter" | "function_call"
    logprobs: object | null
usage:
  prompt_tokens:     integer
  completion_tokens: integer
  total_tokens:      integer
system_fingerprint: string       # optional
```

**AuditTrace-AI emits this shape byte-for-byte today** — verified by
`tests/test_openai_compatibility.py::TestSuccessShape::test_non_streaming_success_matches_openai_chat_completion`.
Pass-through from the upstream llama-server per ADR-024 (dict
pass-through, no strict Pydantic schema).

### 2. `CreateChatCompletionStreamResponse` (SSE chunk)

Canonical shape (see `openapi.yaml` line ~37423):

```yaml
id:      string                        # required
object:  "chat.completion.chunk"       # required
created: integer                       # required
model:   string                        # required
choices:
  - index:    integer
    delta:
      role:       "assistant"          # on first frame
      content:    string               # on each content token
      tool_calls: array                # on tool-call frames
    finish_reason: string | null
usage: object | null                   # typically on final frame only
```

Terminated by `data: [DONE]\n\n`.

**AuditTrace-AI forwards these byte-for-byte from the upstream
llama-server** (streaming proxy) and injects one synthetic usage
chunk before `[DONE]` for SDKs that require a final usage frame.
Verified by
`tests/test_openai_compatibility.py::TestSuccessShape::test_streaming_success_frames_parse_as_chunks`.

### 3. `Error` and `ErrorResponse` (failure bodies)

Canonical shape (`openapi.yaml` line ~42006):

```yaml
Error:
  type: object
  required: [type, message, param, code]
  properties:
    type:    string
    message: string
    param:   string | null
    code:    string | null

ErrorResponse:
  type: object
  required: [error]
  properties:
    error: { $ref: Error }
```

**AuditTrace-AI emits this as a strict superset** on both the global
500 handler (`server.py::unhandled_exception_handler`) and the
streaming SSE error frames (`routes/chat.py::_openai_error_body`):

```json
{
  "error": {
    "message": "llama-server timeout after 300s",
    "type": "api_error",
    "param": null,
    "code": "proxy_timeout",

    "status": 504,
    "operator_hint": "Grep memory-server logs in Loki with this trace_id; cross-reference Langfuse observations.",
    "trace_id": "4f1a...",
    "user_facing_message": "Something went wrong. Please try again."
  }
}
```

- **First four keys**: bit-exact OpenAI `Error` shape. Any
  OpenAI SDK parses them unchanged.
- **Last four keys**: AuditTrace-specific. An OpenAI-only client
  ignores them. An operator pivots from `trace_id` into Loki /
  Langfuse / Grafana; a user sees `user_facing_message`; a
  dashboard reads `status` + `code`.

OpenAI's `type` vocabulary we reuse (see `openapi.yaml` error
guides):

| OpenAI `type` | When |
|---|---|
| `api_error` | Server-side failure on our side (timeout, upstream issue, internal error) |
| `invalid_request_error` | Client sent a bad payload (400) |
| `authentication_error` | Missing / invalid bearer token (401) |
| `permission_error` | Token lacks the required scope (403) |
| `not_found_error` | Route or resource not found (404) |
| `rate_limit_exceeded` | Too many requests (429) |

Our `code` values are AuditTrace-specific (`proxy_timeout`,
`upstream_unreachable`, `upstream_error`, `internal_error`, …) and
are scoped as the failure taxonomy in
`routes/chat.py::FAILURE_*` constants.

Verified by
`tests/test_openai_compatibility.py::TestErrorBodyOpenAIShape` and
`tests/test_openai_compatibility.py::TestSSEErrorFrameOpenAIShape`.

## Known gaps (tracked as follow-up)

- `HTTPException` default body uses FastAPI's `{"detail": "..."}`
  shape rather than the OpenAI envelope. Aligning this requires a
  `HTTPException` → OpenAI-envelope converter; scoped under ADR-033
  (task #15) alongside the formal envelope decision doc.

## Why this folder exists

Luis, 2026-04-16: *"strict adherence to the standards of OpenAI to
avoid a trap of a custom system — this is also a big sales pitch."*
The vendored spec turns "don't break compatibility" from a principle
we trust ourselves to remember into an auditable artefact we can
diff against on every PR.
