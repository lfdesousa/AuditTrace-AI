# Evidence — WU-5 same-turn session recall, local capture (2026-09-06, v2 fix pass)

**Scope of this evidence file.** §1-4 below document the pass-1 build
against the ratified v1 spec (`2026-09-06-SPEC-wu5-same-turn-session-
recall.md`, sha256
`a000459d1b4558e287cbbafc0007a1f73d3311839ba314823cfb725b31b827f0`). Pass-1
was REJECTED by the independent reviewer for the reason captured in §6
below; the operator amended + re-ratified the spec as
`2026-09-06-SPEC-wu5-same-turn-session-recall-v2.md` (sha256
`1f60d6cc765f98de441aa05f6935a3776d2cc54492aa652ce4d51476e59c44c6`), and
§5-6 document the surgical fix pass against v2, on the SAME branch/commit
lineage. This is a **LOCAL-only** work unit throughout — `Loop: ADR-059
builder -> independent reviewer; LOCAL gates only; operator merges via
web` and explicit "Out of scope: ... live front-door E2E (ADR-049 Rule
2/3 live evidence for WU-1..5)" — deferred to WU-6. This file satisfies
ADR-049 Rule 1 (Verification) in full and gives a reconstructible
Rule-3-shaped capture (neuter-proof of every non-vacuity
guard named in the spec, through the real production code paths — the
real `PostgresSessionMemoryService`, the real `ChromaSemanticService`, the
real `tools_visible_to`/`invoke_tool` dispatch, and the real
`/v1/chat/completions` tool loop). It explicitly does **NOT** satisfy
Rule 2 (Validation through a deployed image + public API + scoped JWT) —
that is deferred to WU-6 per the spec, mirroring the
`wu1-session-layer-narrow-ingest-scope-local-capture-2026-09-04.md` and
`wu4-promote-session-to-durable-local-capture-2026-09-05.md` precedents.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.71%
4258 passed, 2 warnings in 331.85s (0:05:31)
🔒 Enforcing per-file coverage gate (each component >= 90%)...
per-file coverage gate: PASS (104 files checked, lines >= 90%, branches >= 90% on 93 file(s) with branches)
🚫 Enforcing zero-skip policy...
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed
```

New/touched-file coverage (line + branch), from the full run:

```
src/audittrace/services/session_memory.py     89      0     12      0   100%
src/audittrace/tools/memory_handlers.py      209      1     62      1    99%   608
src/audittrace/tools/__init__.py             118      5     34      1    96%   188-190, 203-204
src/audittrace/auth.py                       139      0     36      1    99%   557->559
```

(Uncovered lines above are pre-existing branches in code this WU did not
touch — `tools/__init__.py:188-190/203-204` is the TOML config-overlay
warning path, `auth.py:557->559` a pre-existing branch tail, both
unrelated to WU-5's additions.)

`make lint` (ruff check + ruff format + offline semgrep security-lint)
green:

```
$ make lint
...
Ran 2 rules on 158 files: 0 findings.
✅ Security-lint passed
All checks passed!
✅ Linting passed
290 files already formatted
✅ Formatting passed
```

`make helm-lint` green (chart touched — the two realm files + the
memory-scopes ConfigMap):

```
$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every touched `src/` file:

```
$ .venv/bin/mypy src/audittrace/services/session_memory.py \
    src/audittrace/tools/memory_handlers.py src/audittrace/tools/__init__.py \
    src/audittrace/auth.py
Success: no issues found in 4 source files
```

`mypy src/` (full project) surfaces exactly ONE pre-existing error,
verified identical on `main` (no diff to this file from this WU):

```
$ .venv/bin/mypy src/
src/audittrace/services/trust_store.py:610: error: Missing positional
argument "validation_time" in call to
"_validate_and_extract_tl_data_multiple_certs"  [call-arg]
Found 1 error in 1 file (checked 109 source files)
```

(`git diff --stat main -- src/audittrace/services/trust_store.py` is
empty — this WU never touches that file; the same error reproduces when
`mypy` is run from the un-worktreed `main` checkout. Pre-existing, out of
scope for WU-5.)

## 2. OpenAPI drift gate — additive-only confirmation

```
$ git diff --cached --stat docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml
 docs/reference/audittrace/openapi.yaml | 4 ++++
 tests/fixtures/openapi.snapshot.yaml   | 4 ++++
 2 files changed, 8 insertions(+)
```

Only one new entry added: the `memory:session:read-own` scope description
in the OAuth2 security-scheme `scopes` map. `/v1/chat/completions` is
byte-for-byte unchanged — confirmed via `git diff --cached
docs/reference/audittrace/openapi.yaml | grep -iE "chat|completions|/v1"`
returning no output.

## 3. Neuter-proof — every non-vacuity guard fails RED when broken, GREEN restored

Builder-side falsifiability pass (the independent reviewer re-runs this
mandate-to-fail check itself; this is the builder's own verification that
each guard is real before handing off). Every neuter below was applied
directly to the working file via a scripted single-line edit, confirmed
RED, then restored via the inverse edit — `git status --short` after
every restore shows the working tree byte-identical to the staged content
(no diff at all), confirmed after the LAST restore, immediately before
the final `make test` run in §1 was captured.

### 3.1 `list_own` `user_id` filter (isolation) — spec §6.1 / acceptance (c)

Guard: `.filter(SessionMemoryItem.user_id == user_context.user_id)` in
`PostgresSessionMemoryService.list_own` (`src/audittrace/services/
session_memory.py`). Neutered by removing the `.filter()` clause entirely.

```
$ pytest tests/test_session_memory_service.py::TestPostgresSessionMemoryServiceListOwn::test_list_own_cross_user_isolation \
         tests/test_recall_attachments_tool.py::TestRecallAttachmentsIsolation -q --no-cov
FAILED tests/test_session_memory_service.py::...::test_list_own_cross_user_isolation
FAILED tests/test_recall_attachments_tool.py::...::test_cross_user_upload_never_leaks
2 failed in 0.21s

# restored:
$ pytest tests/test_session_memory_service.py::TestPostgresSessionMemoryServiceListOwn::test_list_own_cross_user_isolation \
         tests/test_recall_attachments_tool.py::TestRecallAttachmentsIsolation -q --no-cov
2 passed in 0.15s
```

### 3.2 Tool scope gate `required_scope="memory:session:read-own"` — spec §6.2 / acceptance (a)

Guard: the `required_scope=` argument on `@register_memory_tool(name=
"recall_attachments", ...)` (`src/audittrace/tools/memory_handlers.py`).
Neutered to `"memory:conversational:read-own"`.

```
$ pytest tests/test_recall_attachments_tool.py::TestRecallAttachmentsScopeGate -q --no-cov
FAILED test_required_scope_is_session_read_own
FAILED test_visible_to_scope_holder
FAILED test_hidden_from_non_holder
3 failed, 1 passed in 0.11s

# restored:
$ pytest tests/test_recall_attachments_tool.py::TestRecallAttachmentsScopeGate -q --no-cov
4 passed in 0.06s
```

### 3.3 Content cap / `truncated` flag — spec §6.3

Guard: the `d.page_content[:_SNIPPET_LIMIT]` slice in `recall_attachments`
(`src/audittrace/tools/memory_handlers.py`). Neutered to `d.page_content`
(unbounded).

```
$ pytest tests/test_recall_attachments_tool.py::TestRecallAttachmentsContentCap -q --no-cov
FAILED test_long_upload_snippet_is_capped_and_flagged
1 failed, 1 passed in 0.20s

# restored:
$ pytest tests/test_recall_attachments_tool.py::TestRecallAttachmentsContentCap -q --no-cov
2 passed in 0.15s
```

### 3.4 `has_more` +1 probe — spec §6.4

Guard: `window = min(offset + limit + 1, MAX_RECALL_WINDOW)` in
`PostgresSessionMemoryService.list_own`. Neutered to
`min(offset + limit, MAX_RECALL_WINDOW)` (drops the +1 probe).

```
$ pytest tests/test_recall_attachments_tool.py::TestRecallAttachmentsPagination \
         tests/test_session_memory_service.py::TestPostgresSessionMemoryServiceListOwn::test_list_own_plus_one_probe_window_size -q --no-cov
FAILED test_has_more_true_when_more_exist
FAILED test_list_own_plus_one_probe_window_size
2 failed, 2 passed in 0.33s

# restored:
$ pytest tests/test_recall_attachments_tool.py::TestRecallAttachmentsPagination \
         tests/test_session_memory_service.py::TestPostgresSessionMemoryServiceListOwn::test_list_own_plus_one_probe_window_size -q --no-cov
4 passed in 0.27s
```

### 3.5 Cross-user promoted-durable read scoping — spec §6.5 / acceptance (d)

Guard: `if not user_context.is_admin: where = {"user_id": ...}` in
`ChromaSemanticService.search_page` (`src/audittrace/services/
semantic.py`) — the EXISTING mechanism WU-5's D4 proves, not new code.
Neutered `if not user_context.is_admin:` to `if False:` (search_page never
applies the per-user filter, mirroring "every caller becomes admin-like").

```
$ pytest tests/test_wu5_promoted_durable_recall.py -q --no-cov
FAILED test_cross_user_recall_semantic_never_sees_others_promoted_doc
1 failed, 1 passed in 0.25s

# restored:
$ pytest tests/test_wu5_promoted_durable_recall.py -q --no-cov
2 passed in 0.20s
```

All five neuter/restore cycles verified working-tree byte-identical to
the pre-neuter (staged) state after restore (`git status --short` showing
only the originally-staged file set, zero unstaged diff) before the final
`make test` run (§1) was captured.

## 4. Frozen invariants — spot checks

- `/v1` byte-inviolate: §2 above — the OpenAPI diff is a pure addition
  (the new scope's security-scheme entry); zero lines touched under any
  `/v1/chat/completions` path. `tests/test_wu5_same_turn_recall.py::
  TestSameTurnAuditRowTraceability::test_tool_call_produces_audited_and_traceable_row`
  proves the FULL `/v1/chat/completions` tools-mode round trip still
  produces the exact same response shape (`choices[0].message.content`)
  with the new tool present in the loop.
- A user WITHOUT `memory:session:read-own` gets a byte-identical `tools`
  array: `tests/test_wu5_same_turn_recall.py::TestSameTurnToolVisibility::
  test_non_holder_does_not_see_recall_attachments` and
  `tests/test_recall_attachments_tool.py::TestRecallAttachmentsScopeGate::
  test_hidden_from_non_holder`.
- Traceability: `tests/test_wu5_same_turn_recall.py::
  TestSameTurnAuditRowTraceability` proves the `ToolCall` row carries
  token-derived `user_id` (`SENTINEL_SUBJECT`) and
  `granted_scope="memory:session:read-own"`, and links via
  `interaction_id` to an `InteractionRecord` carrying `session_id` (real,
  request-derived) and `trace_id` (pinned via the same
  `_current_trace_id_hex` monkeypatch technique
  `test_chat_failure_audit.py::TestPersistTraceIdBinding` established,
  since the TestClient does not reliably carry an ambient OTel span the
  way a real deployed request does).
- Session has no durable footprint: `list_own`/`recall_attachments` never
  touch ChromaDB or any indexer — grep confirms zero
  `chromadb`/`upsert`/`index` references in
  `src/audittrace/services/session_memory.py`.
- Never trust caller metadata: `list_own`'s `user_id` filter is always
  `user_context.user_id` (token-derived via `require_user`), never a
  request/args field — `recall_attachments`'s `args` schema has no
  `user_id`/`filename`-scoping property at all, only `n`/`offset`.

## 5. Scope grant type — RATIFIED as v2 Amendment A (was a documented deviation in pass-1)

The v1 spec's D3 item (4) literally said "grant OPTIONAL on the
`audittrace-librechat` client", following the WU-1 `memory:session:write`
pattern, while item (5) required the scope to reach the `/v1` tool loop
"the same way `memory:semantic:read` does" (a DEFAULT binding) — an
internal contradiction. Pass-1 resolved it by binding
`memory:session:read-own` as **DEFAULT**, flagged explicitly as a spec
deviation per the BUILDER contract's "note it" instruction. The
independent reviewer's pass-1 REJECT was NOT on this point (isolation, the
scope gate, `has_more`, and the promoted-durable guard were all confirmed
non-vacuous) — the operator separately amended + re-ratified the spec as
`2026-09-06-SPEC-wu5-same-turn-session-recall-v2.md`
(sha256 `1f60d6cc765f98de441aa05f6935a3776d2cc54492aa652ce4d51476e59c44c6`),
**Amendment A**, which now RATIFIES the DEFAULT binding outright — no
longer a builder judgment call. Traced in code comments
(`src/audittrace/auth.py`, both realm files, the ConfigMap script,
`scripts/setup-memory-scopes.sh`) and in
`tests/test_chart_drift_guards.py::TestKeycloakSessionReadOwnScopeGovernance`
— reasoning kept for the record:

1. `memory:semantic:read` (the item-5 comparator) is itself a DEFAULT
   scope on `audittrace-librechat`, not optional.
2. Traced mechanism: the console chat path's RFC 8693 exchange
   (`bff/exchange.py::exchange_token`, `requested_scope=None`) mints a
   token carrying ONLY the target client's DEFAULT scopes — an
   optional-only grant on `memory:session:read-own` would never reach the
   minted chat token through the real console path.
3. Precedent: every OTHER read-own/read scope on `audittrace-librechat`
   is DEFAULT; only WRITE scopes are optional-only.

Fail-closed reasoning: DEFAULT here does not widen risk — it is a
READ-OWN scope (a caller can only ever see their OWN uploads, per the
guard in §3.1), the same risk profile as `memory:conversational:
read-own`, already DEFAULT. **No code change was needed for this
v2 fix pass** — confirmed by re-running
`TestKeycloakSessionReadOwnScopeGovernance` +
`TestSessionReadOwnScopeJobRenderedBinding` (both still green, still
asserting DEFAULT not OPTIONAL) and grepping the realm files/ConfigMap/
script for any stray "optional" binding of this scope (none found).

## 6. Fix pass (2026-09-06, v2) — Amendment B: per-item `content_truncated`

The independent reviewer REJECTED the pass-1 build on a live-proven
finding: the `truncated` field `recall_attachments` emitted was only the
response-level `has_more` pagination alias (inherited verbatim from
`recall_recent_sessions`'s canonical shape) — it said nothing about
whether any ONE match's own content had been cut down to fit the snippet
cap. A 650-char upload capped at 400 chars reported `truncated=False`
(single result, no pagination overflow), even though that exact match's
content WAS truncated. The operator amended the spec (Amendment B,
v2 §4/§6.3) to require a NEW, DISTINCT per-item boolean
`content_truncated` on each match.

**Fix** (`src/audittrace/tools/memory_handlers.py::recall_attachments`):
added `"content_truncated": len(d.page_content) > _SNIPPET_LIMIT` to each
match dict, computed from the RAW (un-capped) content length — `True` iff
that item's own content exceeded the cap, `False` otherwise. The
response-level `truncated`/`has_more` pagination alias is byte-unchanged.

**Non-vacuity (guard 3, re-proven):**

```
$ pytest tests/test_recall_attachments_tool.py::TestRecallAttachmentsContentCap -q --no-cov
3 passed in ...

# neutered: content_truncated hardcoded to False unconditionally
$ pytest tests/test_recall_attachments_tool.py::TestRecallAttachmentsContentCap -q --no-cov
FAILED test_over_cap_upload_reports_content_truncated_true
1 failed, 2 passed in ...

# restored:
$ pytest tests/test_recall_attachments_tool.py -q --no-cov
17 passed in ...
```

Test fix (`tests/test_recall_attachments_tool.py::
TestRecallAttachmentsContentCap`): replaced the pass-1 test (which only
asserted `"truncated" in result` — response-level key presence, which is
always true regardless of content length, hence vacuous against this
exact defect) with three VALUE-asserting tests: an over-cap upload
(`_SNIPPET_LIMIT + 250` chars) must report `content_truncated=True`; an
under-cap upload must report `content_truncated=False`; and a boundary
case at EXACTLY `_SNIPPET_LIMIT` chars must report `False` (strictly-
greater-than, not greater-or-equal — proves the comparison operator
itself, not just its direction). Also added a `content_truncated` value
assertion to `TestRecallAttachmentsCanonicalShape` for the two ordinary
(short) uploads it already seeds.

**Full gate re-run** (isolated worktree venv, `import audittrace`
resolves into the worktree — confirmed):

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.71%
4259 passed, 2 warnings in 438.78s (0:07:18)
per-file coverage gate: PASS (104 files checked, lines >= 90%, branches >= 90% on 93 file(s) with branches)
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed

$ make lint
Ran 2 rules on 158 files: 0 findings.
All checks passed!
290 files already formatted

$ make helm-lint
1 chart(s) linted, 0 chart(s) failed
✅ vaultSecretFileGuard present in 3 workloads

$ .venv/bin/mypy src/
src/audittrace/services/trust_store.py:610: error: ... [call-arg]
Found 1 error in 1 file (checked 109 source files)
```

The single `mypy` error is the SAME pre-existing, unrelated error
identified in the pass-1 capture (§1) — `trust_store.py` is untouched by
this WU on both passes; verified reproducing identically on `main`.

## 7. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the
public API with a scoped JWT against a deployed image — this WU is
LOCAL-only by ratified spec. Live E2E is explicitly deferred to WU-6 +
operator go, per the spec's "Out of scope" section.
