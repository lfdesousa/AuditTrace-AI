# Evidence — M3 slice 1, deterministic sources trailer, local capture (2026-08-24)

**Scope of this evidence file.** This documents the LOCAL, non-deployed verification
performed while building `src/audittrace/routes/_sources_trailer.py` + the two call
sites in `src/audittrace/routes/chat.py` (non-streaming `_handle_tools_mode` return
path + the tools-mode streaming generator) + the `AUDITTRACE_RESPONSE_SOURCES`
config flag. It satisfies ADR-049 Rule 1 (Verification) in full and gives a
reconstructible Rule-3-shaped capture of the request/response/audit-row cycle
running through the REAL FastAPI `create_app()` object (not mocked), with the
upstream `llama-server` HTTP calls mocked (per the spec's own build-order: the
sources-trailer WU is scoped BEFORE the infra WUs that stand up a real LibreChat
console — see `2026-08-24-SPEC-m3-librechat-console.md` §8 step 3). **It explicitly
does NOT satisfy Rule 2 (Validation through a deployed image + public API + scoped
JWT, with a LibreChat-rendered response)** — that is DEFERRED to the next candidate
deploy, per the task's own instruction ("live E2E — LibreChat rendering + the
audit-row byte-compare through the front door — DEFERRED to a candidate deploy,
same pattern as the MCP tracks"), mirroring the MCP Phase 2 Track B precedent
(`evidence/mcp-phase2-track-b-broker-local-capture-2026-08-23.md`) and the #290
pattern. This file is the builder-side artefact that stands in for the PR-body
Validation/Reconstruction sections until a real PR exists (the builder role never
pushes/opens a PR — SDLC-ADR-000 §invariants).

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.83%
3602 passed, 3 warnings in 172.71s (0:02:52)
🔒 Enforcing per-file coverage gate (each component >= 90%)...
per-file coverage gate: PASS (89 files checked, lines >= 90%, branches >= 90% on
80 file(s) with branches)
🚫 Enforcing zero-skip policy...
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed
```

New/touched-file coverage (line + branch), from the same run:

```
src/audittrace/routes/_sources_trailer.py    38      0     16      0   100%
src/audittrace/routes/chat.py               741     18    218      2    97%
src/audittrace/config.py                    164      1     16      1    99%
```

(`chat.py`/`config.py` residual misses are pre-existing, unrelated lines —
confirmed via `git diff` showing only additive edits at the two trailer call sites
and the one new `response_sources` field.)

`ruff check src/ tests/`, `ruff format --check src/ tests/`, and `mypy` on every
file this build touched (`_sources_trailer.py`, `chat.py`, `config.py`,
`tests/test_sources_trailer.py`) are all clean. `make lint` (ruff + the offline
semgrep security-lint gate) passes with 0 findings. The one remaining repo-wide
`mypy` finding (`services/trust_store.py:610`) is pre-existing and unrelated —
confirmed via `git diff --stat`, that file is not part of this change.

## 2. OpenAPI drift gate — zero drift confirmed

```
$ pytest tests/test_openapi_drift.py -q
4 passed
```

`/v1/chat/completions` declares no Pydantic response model (ADR-024 raw-dict
pass-through) and this change adds no new route, query param, or response model —
only a config field and in-process dict/SSE-frame mutation — so the FastAPI-
generated OpenAPI schema is byte-identical to before this change.

## 3. Neuter → RED proofs (falsifiability, before this file was written)

Each guard below was temporarily broken, confirmed RED, then restored and
reconfirmed GREEN (`git diff` after restoration showed the working tree back to
exactly the intended 2-file diff — no residual neuter markers):

1. **Flag-off byte-identical (non-streaming).** Changed the guard from
   `settings.response_sources == "trailer"` to `True` — the trailer now emits
   unconditionally. `TestSourcesTrailerFlagOffByteIdentical::
   test_non_streaming_content_unchanged` went RED (`assert None ==
   'Based on ADR-009: 75% reduction.'` — the trailer text leaked into what the
   test asserts is unchanged content).
2. **Flag-off byte-identical (streaming).** Same neuter applied to the streaming
   call site's guard — `test_streaming_content_unchanged` went RED too (both
   flag-off tests RED with the SAME single-line neuter class, confirming neither
   guard is redundant with the other).
3. **Gate 4 — trailer must equal the recorded audit row.** Hard-coded
   `source_refs = ["FABRICATED-NEUTER-TEST.pdf"]` after the real extraction call
   inside `build_sources_trailer`. SIX tests went RED: 4 unit tests
   (`test_single_match_renders_label_and_bullet`,
   `test_multiple_matches_across_rows_dedup_first_seen_order`,
   `test_cache_hit_shaped_result_still_contributes`,
   `test_malformed_json_is_skipped_not_raised`) plus both integration byte-compare
   tests (`TestSourcesTrailerFlagOnGate4::
   test_non_streaming_trailer_matches_recorded_audit_rows` and
   `test_streaming_trailer_matches_recorded_audit_row`).
4. **Not-LLM-generated (structural).** Added `import httpx` to
   `_sources_trailer.py` — `TestNotLlmGenerated::
   test_module_has_no_network_or_llm_client` went RED.
5. **Not-LLM-generated (integration, extra-call count).** Inserted an extra
   `async with httpx.AsyncClient() as c: await c.post(llama_url, json={...})`
   immediately before the trailer is built at the non-streaming call site —
   `test_non_streaming_trailer_matches_recorded_audit_rows`'s
   `assert len(fake.post_calls) == 3` went RED (`assert 4 == 3`), proving the
   upstream-POST-count assertion actually catches a second generation call, not
   just asserting a number nobody would notice drift on.

After each neuter, `git diff` was inspected to confirm ONLY the deliberate,
single-purpose mutation was present (no accidental co-mutation), the targeted
test(s) were re-run to confirm RED, the file was restored verbatim, and the full
`test_sources_trailer.py` suite (19 tests) plus the broader chat/tool-loop/
openai-compat/config suites (262 tests) were re-run GREEN before proceeding.

## 4. Local end-to-end capture through the real FastAPI app (Rule 3 shape)

Run against `audittrace.server.create_app()` (the actual production factory) via
`fastapi.testclient.TestClient`, with `dependencies.container =
create_test_container()` (in-memory SQLite `InMemoryPostgresFactory` — real
SQLAlchemy `InteractionRecord`/`ToolCall` rows, not mocked) seeded with two real
ADR documents (`ADR-009.md`, `ADR-010.md`) in the mock semantic `decisions`
collection. Upstream `llama-server` calls are mocked (two turns: a
`recall_decisions` tool call, then a final text answer) — the same "fake/stub
downstream" pattern the MCP Phase 2 Track B evidence used for its downstream MCP
server. `AUDITTRACE_AUTH_REQUIRED` unset → bypass-mode admin sentinel identity
(`00000000-0000-0000-0000-000000000001`).

### 4a. Flag OFF (`AUDITTRACE_RESPONSE_SOURCES=off`, the default)

```
POST /v1/chat/completions {"model":"qwen3.5-35b","messages":[{"role":"user",
  "content":"what about KV cache?"}],"project":"AuditTrace"}
→ 200 content: "Final grounded answer."
  upstream POST count: 2   (one tool-call turn + one final-answer turn — no
                             third call for the trailer)
  interactions.answer (persisted): "Final grounded answer."
  tool_calls row: {"tool_name": "recall_decisions",
    "granted_scope": "memory:episodic:read", "error": null,
    "result_summary": "{\"matches\": [{\"id\": \"ADR-009.md\",
      \"source_ref\": \"ADR-009.md\", ...}, {\"id\": \"ADR-010.md\",
      \"source_ref\": \"ADR-010.md\", ...}], \"total\": 2, ...}"}
```

The client-facing content is byte-identical to what it would be with the trailer
code path absent entirely — no "Sources consultées" substring anywhere in the
response — even though a real recall happened and produced real, citable matches
recorded in the audit row.

### 4b. Flag ON (`AUDITTRACE_RESPONSE_SOURCES=trailer`)

```
POST /v1/chat/completions (same request)
→ 200 content: "Final grounded answer.\n\n**Sources consultées**\n- ADR-009.md\n- ADR-010.md"
  upstream POST count: 2   (unchanged from 4a — the trailer added ZERO upstream
                             calls; it is rendered in-process from the recorded
                             tool_calls, never asked of the model)
  interactions.answer (persisted): "Final grounded answer."   (UNCHANGED from
                             4a — the trailer never enters the audit-record
                             answer field, only the client-facing body)
  tool_calls row: identical result_summary to 4a (same recall, same audit row —
                             the trailer flag changes RENDERING only, never what
                             gets recorded)
```

### 4c. Gate-4 byte-compare (the falsifiable claim)

```
recorded source_refs from tool_calls.result_summary (parsed JSON, this
  interaction's rows): ['ADR-009.md', 'ADR-010.md']
expected_trailer = "\n\n**Sources consultées**\n- ADR-009.md\n- ADR-010.md"
content_on == "Final grounded answer." + expected_trailer  →  True
"Sources consultées" not in content_off                    →  True
```

The trailer's bytes are reconstructed directly from the persisted `ToolCall.
result_summary` column — not compared against a fixture the test also wrote — and
match exactly. Capture script: `scratchpad/capture_sources_trailer.py`
(session-local, not committed — the transcript above is the durable artefact).

## 5. Reconstruction chain proven locally

The trailer never diverges from the audit trail because it is built from the SAME
in-memory `PendingToolCall` records the request handler flushes verbatim into the
`tool_calls.result_summary` column (`_execute_memory_tool` in
`_memory_tool_loop.py`, unchanged by this build) — §4c's byte-compare is a
structural guarantee, not a coincidence of the test fixture. §5's "answer
(persisted) unchanged" confirms the trailer is a pure rendering concern layered on
top of the existing tamper-evident write path (ADR-037/058), never a second,
competing source of audit truth.

## 6. What is explicitly NOT covered here (Rule 2 — deferred)

- No image was built or deployed for this evidence.
- No request was sent through the Istio front door with a real Keycloak-issued
  JWT.
- No real llama-server / Qwen model was involved — the tool-call + final-answer
  turns are mocked upstream responses (this is a rendering-layer feature over
  already-audited data; the recall pipeline itself is unchanged and already
  covered by the existing `recall_decisions`/ADR-060 test suite).
- No LibreChat instance rendered the markdown trailer visually — that requires
  the infra WUs (compose services + `librechat.yaml`, spec §8 steps 1-2) which
  are explicitly out of scope for this WU per the task instructions ("build §3 +
  Deliverable §4.4 only").
- No trace was exported to Tempo/Langfuse (local dev has
  `AUDITTRACE_OTLP_ENDPOINT=""`, the laptop no-op default).

These are the ADR-049 Rule 2 (Validation) artefacts the task defers to the next
candidate deploy cycle (the CD-deployer + deploy-verifier agents, not the
builder). When that deploy happens, capture: a `/v1/chat/completions` request
through the front door with a scoped JWT and `AUDITTRACE_RESPONSE_SOURCES=trailer`
set, the resulting trailer text, the linked `tool_calls` row ids and their
`result_summary` source_refs (byte-compared against the trailer), and — once the
LibreChat console infra WUs land — a screenshot of the rendered markdown list in
the LibreChat UI. Supersede this file at that point.
