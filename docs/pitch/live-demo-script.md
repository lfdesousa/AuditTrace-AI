# AuditTrace-AI — live demo script

**Your personal working copy.** Edit freely. This is the presenter's notes for walking a non-trivial audience (head of EA, PhD supervisor, CTO, senior architect) through the running system end-to-end.

---

## Pre-demo checklist (15 min before)

1. **Stack health.**
   ```bash
   export KUBECONFIG=~/.kube/config
   kubectl -n audittrace get pod
   # Expect: all pods 2/2 Running (except otel-collector 1/1 — no istio sidecar)
   ```

2. **Token fresh.**
   ```bash
   scripts/audittrace-login
   # Complete Device Flow in the browser. tokens.json refreshes.
   ```

3. **Fire one warm-up probe** so Tempo's service-map has fresh edges.
   ```bash
   BEARER=$(scripts/audittrace-login --show)
   curl -sk -H "Authorization: Bearer $BEARER" -H "X-Project: warmup" \
     -H "Content-Type: application/json" \
     -X POST https://audittrace.local:30952/v1/chat/completions \
     -d '{"model":"qwen3.6-35b-a3b","stream":false,"messages":[{"role":"user","content":"ready"}],"max_tokens":5}' \
     -w "\nHTTP %{http_code} in %{time_total}s\n"
   ```

4. **Browser tabs (pin in this order):**
   - **Tab 1** — Langfuse: `http://localhost:3000` → Traces view, User filter pre-set to your sub
   - **Tab 2** — Grafana → Explore → Tempo (paste the warm-up trace_id)
   - **Tab 3** — Grafana → Dashboards → Sovereign AI Operations
   - **Tab 4** — Grafana → Dashboards → AuditTrace-AI — Call Flow (Tempo)
   - **Tab 5** — Terminal with `BEARER` already exported
   - **Tab 6** — `docs/reconstructibility-walkthrough.md` rendered on GitHub, for reference if you need to quote text

5. **Screen size + font.** Tempo flamegraph needs 1600px+ wide. Bump terminal font to 16pt so curl output is legible on the projector.

---

## The 15-minute demo (CTO / head of EA / senior architect)

**Message in one sentence to close on:** *"Every LLM request in your enterprise is reconstructible end-to-end, from Keycloak sub down to the MinIO object fetched — today, not in some future roadmap."*

### 00:00 — Set the context (2 min)

*"You've seen a dozen LLM reference architectures this quarter. Most of them draw a pretty diagram and hand-wave over the audit story. I'll show you the audit story first, and the architecture falls out of it."*

Open **Tab 3 — Sovereign AI Operations dashboard.** Point at the three panels that matter to an operator:
- Request Latency P50/P95/P99 — SLO posture at a glance
- HTTP Server Request Rate — volume by route and status code
- Container Logs (ERROR level) — anything going wrong surfaces here

*"If any of these go red, we have a problem. Right now they're green. That's the 'normal day' view. Let me show you what happens when one user asks one question."*

### 02:00 — Fire the probe (1 min)

Switch to **Tab 5 (Terminal).**

```bash
curl -sk -H "Authorization: Bearer $BEARER" \
  -H "Content-Type: application/json" \
  -H "X-Project: live-demo" \
  -X POST https://audittrace.local:30952/v1/chat/completions \
  -d '{
    "model": "qwen3.6-35b-a3b",
    "stream": false,
    "messages": [{"role": "user", "content":
      "What did ADR-027 decide about memory storage? Two short sentences."
    }]
  }' | jq '.choices[0].message.content'
```

*"That's a standard OpenAI-compatible call. Same request OpenCode, Continue, LangChain — any SDK — would make. Nothing about the client knows it's talking to us and not to OpenAI. Now I'll show you what the system knows."*

### 03:00 — Hop 1: Postgres `/interactions` (2 min)

```bash
curl -sk -H "Authorization: Bearer $BEARER" \
  "https://audittrace.local:30952/interactions?project=live-demo&limit=1" | jq
```

**Point at:**
- `user_id` — the Keycloak sub, tamper-proof JWT signature.
- `question` and `answer` — full content, not summarised.
- `status` — success vs `failed` classification (ADR-033). *"If the upstream LLM errored, this row would still exist with `status='failed'` and a controlled-vocabulary `failure_class`. No silent failures."*
- `duration_ms` and token counts — cost + SLO signals per request.

*"That's the structured audit row. It hits Postgres synchronously on every request. Row-Level Security enforces that even a bug in my service code can't leak another user's row — the database itself refuses."*

### 05:00 — Hop 2: Langfuse — the LLM view (4 min)

Switch to **Tab 1 — Langfuse.** User filter already set to your sub; the live-demo probe should be at the top.

**Click into the trace.** Show the tree in the Timeline tab.

*"This is the LLM's view of the same request. Three generation rounds — the model consulted memory tools between rounds. See that nested `memory_tool.recall_decisions` call? That's the model deciding at runtime that it needs architectural context. We didn't inject it up-front; the model asked for it. Each tool call gets its own span with the args the model passed in and the result the tool returned."*

**Click the root `POST /v1/chat/completions`.** Point at Input/Output populated.

*"Input panel: the messages the caller sent. Output panel: the answer. This is what an auditor clicks in 20 seconds to see 'what did this user ask and what did we respond'."*

**Click one `llm.chat.completions` child.** Show the Generation card.

*"This is the actual LLM inference as Langfuse renders it. Model name, prompt, completion, token usage. The generation card is Langfuse's first-class object for LLM calls. Cost per request, token-level observability — standard LLM-ops tooling."*

### 09:00 — Hop 3: Tempo — the engineering view (3 min)

Switch to **Tab 2 — Tempo flamegraph** (already showing the warm-up trace; paste the new trace_id and refresh).

**Point at the flamegraph.**

*"Same request, now rendered as 74 OpenTelemetry spans. Every outbound call the memory-server made is visible. See that big bar — `llm.chat.completions` at about a minute? That's the LLM. Everything else — Keycloak JWKS, Postgres writes, Redis cache, Langfuse ingestion — all microseconds. Our overhead is 1 millisecond; the only slow thing is the model, which is the trade-off the user chose."*

Switch to **Tab 4 — Service Map.**

*"And this is the service graph — which systems did the memory-server talk to on this request. Named edges for every peer: Postgres, ChromaDB, MinIO, Redis, Keycloak, the three LLMs, Langfuse. Per-edge latency + request-rate + success/failed. No unnamed IP nodes."*

### 12:00 — The trust boundary (1 min)

Switch back to the terminal or the GitHub-rendered walkthrough.

*"One more thing — and this is the part that tells you I've actually deployed this in production, not just diagrammed it. We explicitly do **not** audit agent-side tools. Bash, file-read, edit, grep — those run on the user's machine, we never see the results. Claiming we audit them would be compliance theatre. We document this as ADR-037; the honest boundary is on the slide, not hidden."*

### 13:00 — The ask (2 min)

*"Two things I'd value from you:*

1. *Where does this framing land wrong for your audit function? I want to hear the objection before it reaches procurement.*
2. *What's the next step — reference architecture reader, pilot conversation, joint paper, introduction to your audit lead?*

*Everything you've seen is in the GitHub repo — I'll send the link. The walkthrough you just followed is a 15-minute read with all the commands. You can reproduce it against my running cluster or stand up your own in an afternoon."*

---

## The 30-minute demo (PhD supervisor / formal-methods academic)

Same shape as the 15-minute version, but extend three sections:

**After Hop 1 — the RLS deep-dive (+3 min).** Open `src/audittrace/migrations/versions/005_enable_rls_policies.py`. Walk through the `FORCE ROW LEVEL SECURITY` + `WITH CHECK` policy. Point at `tests/test_rls_isolation.py::test_alice_cannot_insert_as_bob` — *"the positive test of the WITH CHECK clause; we actively provoke a violation to prove it fires."* Then point at the dedicated `audittrace_summariser` role (BYPASSRLS, minimum grants) — *"the one place we explicitly weaken RLS, scoped to one role that only the background summariser uses, and audited via Helm hook + tests."*

**After Hop 3 — the evaluation framework (+4 min).** Open `docs/eval-memory-modes-*.md`. Walk through the N=100 eval methodology. Show the comparison between `inject` vs `tools` mode — latency, reliability, tool-selection accuracy. *"This is what a reconstructibility benchmark looks like when operationalised. Every number here is reproducible; the JSONL outputs are in the repository."*

**After the trust boundary — the research framing (+5 min).** Open `docs/phd/research-demonstrator-framing.md`. Walk the reviewer through the four candidate PhD directions. Pause on direction 1 (formal specification of reconstructibility in PRISM-compatible temporal logic) if the audience is Liverpool / Dr. Schewe specifically — that connects to Liverpool's formal-methods tradition.

**Close on (3 min):**
1. *"What's the right scope for a PhD thesis — one of the four directions, or a combination?"*
2. *"Which existing Liverpool supervisor / research group does this align with best?"*
3. *"What would you need to see before recommending admission — a stronger empirical chapter, a formal-methods prototype, a literature review on the sovereignty-reconstructibility gap?"*

---

## Anticipated pushback — one-liner rebuttals

Keep these in a second terminal / sticky note. If asked:

- *"Isn't this just OpenTelemetry + a FastAPI proxy?"* — OTel gives you the spans; the contribution is the architectural commitment that **every LLM interaction produces a complete reconstructibility bundle across four stores linked by a common identifier triple**. Without the commitment, OTel gives you hints. With it, you have audit.

- *"What about the cost of storing every trace?"* — ~150 MB/day/user at 1000 req/day. Linear. A 10 000-user firm storing 90 days of audit = 135 GB. That's a desktop hard drive, not a CFO conversation.

- *"Doesn't RLS fall apart at scale?"* — RLS is a correctness boundary, not a performance boundary. Reads stay indexed; writes are a single GUC set per transaction. The bottleneck at scale is the LLM, not the database.

- *"What if OpenAI changes their API?"* — We vendor the current OpenAI OpenAPI spec (`docs/reference/openai/openapi.yaml`, 73k lines, SwissStainless-hosted upstream). Regression tests fail CI on any divergence. We're strict-superset-compatible: every field they add flows through; every error shape they ship, we match.

- *"What's the play against Bedrock / Azure AI / Vertex?"* — Those are sovereignty + reconstructibility *losses* dressed up as SaaS convenience. Your data flows out of the jurisdiction to be inferred. AuditTrace-AI keeps both model and data on-prem; the commercial stacks can't make that claim structurally, not just contractually.

- *"How long did this take to build?"* — One architect, weekends + evenings, ~6 weeks of focused effort (2026-Q1), on top of 20 years of prior financial-services production experience. The architectural instinct isn't new; the implementation is.

- *"Why not LangChain / LlamaIndex?"* — They're frameworks optimised for developer productivity, not for audit. Memory-as-tools (ADR-025) is a different architectural primitive: explicit per-turn tool invocations by the LLM itself, each one an auditable event. Frameworks hide the calls; we surface them.

- *"What's the licence?"* — Engine AGPL v3, framework / ADRs retained IP of allaboutdata.eu. Open enough for academic use and community contribution, restrictive enough that a commercial fork has to open-source their changes.

---

## What NOT to say

- **Never promise scale numbers you haven't measured.** "We can handle millions of users" → if asked, "we've measured 1 user on 1 node; horizontal scaling is architectural, not measured at that scale yet."
- **Never claim formal verification where there is none.** "We have tests" ≠ "we have proofs." Don't imply the latter.
- **Never disparage the commercial alternatives.** "OpenAI is reckless" → wrong frame. "OpenAI serves a different audience; regulated industries need a different tool." Head of EA has a production OpenAI contract; dignity matters.
- **Never oversell the agent-side story.** The limitation is clear (ADR-037). Admitting it builds trust; hiding it loses trust.

---

## Recording logistics (if you want a video walkthrough)

- **OBS Studio**, 1920x1080, 30 fps, webcam overlay bottom-right
- **Audio**: a proper mic (not laptop built-in) — head of EA will watch this on a decent setup.
- **Chapters**: record each section separately, stitch in post. Re-record individual takes is cheaper than re-doing the whole thing.
- **15-min target length** for the version you send unsolicited; **30-min target** for a version you send after an expressed interest.
- Upload to a private unlisted YouTube / Vimeo; send the link with the repo.

---

## After the demo — follow-up kit

Pre-stage these so you can send in two clicks:

**Email 1 (within 24h):**
> *Thanks for the time. As mentioned: GitHub repo is [link]. The walkthrough is `docs/reconstructibility-walkthrough.md` — a 15-minute read that reproduces everything we walked through live. Happy to pair on any of the architectural decisions; the ADRs in `docs/` are numbered and each covers one commitment with its trade-offs explicit.*
>
> *Two specific asks: (1) the objection I should have anticipated but didn't, and (2) the next step that would be most useful from your side — a reader's review of the doc, an intro to your audit team, a pilot scope conversation, something else.*

**Email 2 (if silence after 1 week):**
> *Following up lightly — no pressure. If the doc didn't land for you, that's useful data too. The framing is deliberately testable; telling me it's off is the fastest path to a better version.*

That's it. The demo is short because the system earns the pitch in the first three hops; everything after is gravy.
