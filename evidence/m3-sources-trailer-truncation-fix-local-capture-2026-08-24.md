# Evidence — M3 sources-trailer TRUNCATION FIX, local capture (2026-08-24)

**Scope of this evidence file.** Documents the LOCAL, non-deployed verification
performed while fixing the M3 sources trailer's truncation bug
(`finding-m3-sources-trailer-truncation-inert-20260824.md` /
[[project_m3_sources_trailer_truncation_bug]]): the trailer re-derived
`source_refs` from `PendingToolCall.result_summary`, but that column is
truncated to 1000 chars (`_memory_tool_loop.py`, `summary =
json.dumps(result)[:1000]`), so a realistic multi-match recall's serialized
result exceeds the cap, the stored summary is invalid JSON, strict
`json.loads` raises, every match is skipped, and the trailer renders NOTHING
— found live on v1.24.9 (interaction 3000). Fix: extract `source_refs` from
the FULL, pre-truncation tool result and carry them structurally on
`PendingToolCall.source_refs`; the trailer reads that field and never
re-parses `result_summary`. Satisfies ADR-049 Rule 1 (Verification) in full
and gives a reconstructible Rule-3-shaped capture through the REAL FastAPI
`create_app()` (not mocked), with only the upstream `llama-server` HTTP calls
mocked — same pattern as the original #301 evidence file
(`evidence/m3-sources-trailer-local-capture-2026-08-24.md`). **Does NOT
satisfy Rule 2 (Validation through a deployed image + public API + scoped
JWT)** — DEFERRED to the next candidate deploy per the spec's own
instruction ("re-run the ephemeral live test that found the bug"), mirroring
the #301 / MCP Phase 2 Track B precedent.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.83%
3608 passed, 3 warnings in 256.20s (0:04:16)
✅ Tests passed
```

Touched-file coverage (line + branch), from the same run:

```
src/audittrace/routes/_memory_tool_loop.py   295      3     94      0    99%   301-302, 436 (pre-existing, unrelated)
src/audittrace/routes/_sources_trailer.py     24      0     10      0   100%
```

`ruff check`, `ruff format --check`, and `mypy` on both touched source files
plus `tests/test_sources_trailer.py` are all clean:

```
$ ruff check src/audittrace/routes/_memory_tool_loop.py src/audittrace/routes/_sources_trailer.py tests/test_sources_trailer.py
All checks passed!
$ ruff format --check <same three files>
3 files already formatted
$ mypy src/audittrace/routes/_memory_tool_loop.py src/audittrace/routes/_sources_trailer.py
Success: no issues found in 2 source files
```

## 2. OpenAPI drift gate — zero drift confirmed

```
$ pytest tests/test_openapi_drift.py -q
4 passed
```

No route, query param, or response model changed — `PendingToolCall` is an
internal dataclass never exposed through the OpenAPI schema (ADR-024
raw-dict pass-through on `/v1/chat/completions`), and the flag-gating logic
in `chat.py` is unchanged by this fix. Every pre-existing flag-off /
byte-identical test (`TestSourcesTrailerFlagOffByteIdentical`) stays green.

## 3. Neuter → RED proofs (falsifiability)

Each guard below was temporarily broken (by restoring the PRE-FIX
`_extract_source_refs` body — re-derive from `rec.result_summary` via
`json.loads`, dropping the `PendingToolCall.source_refs` read entirely),
confirmed RED, then restored via `cp` from a pre-neuter backup and
reconfirmed GREEN (`git diff` after restoration showed the working tree
back to exactly the intended fix, byte-identical):

1. **THE regression test (the one that would have caught the original
   bug).** `TestSourcesTrailerFlagOnGate4::
   test_non_streaming_trailer_survives_over_1000_char_result` — 3-match
   recall, full serialized result > 1000 chars. Neutered → RED:
   `AssertionError: assert 'Final grounded answer.' ==
   'Final grounded answer.\n\n**Sources consultées**\n- ADR-101.md\n-
   ADR-102.md\n- ADR-103.md'` (the trailer vanished entirely — reproducing
   the exact inert-trailer bug found live on v1.24.9).
2. **Unit-level structural proof.** `TestBuildSourcesTrailerUnit::
   test_garbled_result_summary_is_never_reparsed` — a `result_summary`
   truncated mid-JSON (`'{"matches": [{"source_ref": "ADR-009'`) paired with
   a real `source_refs=["ADR-009.md"]`. Neutered → RED:
   `AssertionError: assert '\n\n**Sources consultées**\n- ADR-010.md' ==
   '\n\n**Sources consultées**\n- ADR-009.md\n- ADR-010.md'` (the row whose
   `result_summary` is garbled JSON dropped out of the trailer once the
   code re-parsed it instead of reading the structural field).
3. **Gate 4 + B5 (pre-existing #301 guards, re-run to confirm they still
   pass — and are non-trivially exercised — after this fix).**
   `test_non_streaming_trailer_matches_recorded_audit_rows`,
   `test_streaming_trailer_matches_recorded_audit_row`, and the new
   >1000-char test's own B5 assertion all stayed GREEN under the fix and
   RED was confirmed to be specific to the truncation-shaped fixtures
   above — the small (<1000-char) #301 fixtures never exercised the
   truncation path, which is exactly the "fixture didn't match the
   production data shape" lesson this bug is the canonical case of.

After each neuter, `git diff` was inspected to confirm ONLY the deliberate
mutation was present, the targeted tests were re-run to confirm RED, the
file was restored via `cp` from a pre-neuter backup (byte-identical
restoration confirmed via `git diff --stat`), and the full
`test_sources_trailer.py` suite (25 tests) was re-run GREEN before
proceeding. Full `make test` (3608 tests, real suite order) reconfirmed
GREEN after restoration.

## 4. Local end-to-end capture through the real FastAPI app (Rule 3 shape)

Run against `audittrace.server.create_app()` (the actual production
factory) via `fastapi.testclient.TestClient`, with
`dependencies.container = create_test_container()` (in-memory SQLite
`InMemoryPostgresFactory` — real SQLAlchemy `InteractionRecord`/`ToolCall`
rows). Seeded THREE decisions (`ADR-101.md`, `ADR-102.md`, `ADR-103.md`)
with long bodies so the recall's FULL serialized result — the realistic
multi-match production shape — exceeds 1000 chars. Upstream `llama-server`
calls mocked (a `recall_decisions` tool call, then a final text answer).
`AUDITTRACE_MEMORY_MODE=tools`, `AUDITTRACE_RESPONSE_SOURCES=trailer`.

```
POST /v1/chat/completions {"model":"qwen3.5-35b","messages":[{"role":"user",
  "content":"what about KV cache?"}],"project":"AuditTrace"}
→ 200
content: 'Final grounded answer.\n\n**Sources consultées**\n- ADR-101.md\n- ADR-102.md\n- ADR-103.md'

tool_calls row count: 1
result_summary length: 1000                         (truncated at the cap, as designed)
result_summary is INVALID JSON at the 1000-char cut: Unterminated string
  starting at: line 1 column 677 (char 676)          (ground truth — this IS
                                                        the real production
                                                        truncation-corruption
                                                        shape, not a fixture
                                                        that happens to stay
                                                        under the cap)

persisted interactions.answer: 'Final grounded answer.'   (B5 — the trailer
  never entered the audit record, even under the real >1000-char rendering
  case)
'Sources consultées' in persisted answer: False
'Sources consultées' in client content: True
```

The trailer renders exactly the recorded `source_refs` (`ADR-101.md`,
`ADR-102.md`, `ADR-103.md`, in the order the mock semantic search returned
them) even though the audit column backing the OLD (pre-fix) extraction
path is genuinely, unambiguously invalid JSON. Capture script:
`scratchpad/capture_trailer_truncation_fix.py` (session-local, not
committed — the transcript above is the durable artefact).

## 5. Reconstruction chain proven locally

`PendingToolCall.source_refs` is populated from the SAME `result` dict the
loop already has in hand before truncating it for `result_summary`
(`_source_refs_from_result(result)`, called immediately before
`json.dumps(result)[:1000]` at both the fresh-call and cache-hit sites in
`_memory_tool_loop.py::_execute_memory_tool`) — so the trailer and the
audit row are still built from identical source data, just via a field that
survives the audit column's intentional truncation instead of a second,
lossy parse of it. No audit-schema change: `result_summary` stays truncated
to 1000 chars exactly as before (confirmed in §4 — `len(row.result_summary)
== 1000`), `source_refs` is in-memory/per-request only and is never written
to the `tool_calls` table.

## 6. What is explicitly NOT covered here (Rule 2 — deferred)

- No image was built or deployed for this evidence.
- No request was sent through the Istio front door with a real
  Keycloak-issued JWT.
- No real llama-server / Qwen model was involved — the tool-call +
  final-answer turns are mocked upstream responses (this is a rendering-
  layer fix over already-audited data; the recall pipeline itself is
  unchanged).
- No trace was exported to Tempo/Langfuse (local dev has
  `AUDITTRACE_OTLP_ENDPOINT=""`, the laptop no-op default).

These are the ADR-049 Rule 2 (Validation) artefacts the spec defers to the
next candidate deploy cycle: re-run the ephemeral live test that found the
bug (flip `AUDITTRACE_RESPONSE_SOURCES=trailer`, ask a corpus-relevant
multi-match question through the front door with a scoped JWT) and capture
the interaction id, the rendered trailer, and the recorded `source_refs`.
Supersede this file at that point.
