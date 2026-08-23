# ADR-063 — MCP entry-interface: the audit layer as an MCP server

- Status: Accepted (implementation phased; pending)
- Date: 2026-08-06
- Relates to: ADR-037 (agent tool-call audit boundary), ADR-058 (recursive self-audit / tamper-evident append-only)

## Origin

This decision was prompted by **Derek Ciula** (AI integration and architecture for regulated,
high-stakes environments), author of **HEARTH** — a compact, local MCP gateway for coding agents
(https://www.steppeintegrations.com/articles/build-your-own-hearth/). In a public discussion he asked
whether we had considered MCP at the user-request stage, pointed at HEARTH, and then named the real
insight himself: the append-only ledger, "every call one ledger event", was the moment that mattered
more than MCP itself. This ADR is the response to that nudge. Same fire, bigger house.

## Context

1. **We already record, but we do not expose an MCP surface.** The memory-server runs an internal
   tool loop today (semantic/decision/skill recall, context reads) and records every tool call it
   executes as a first-class audit event. That surface is reachable through our own chat path, but it
   is not exposed as a standard **Model Context Protocol (MCP)** server that any MCP-speaking agent
   can discover and call.
2. **HEARTH is the reference design, and an honest one.** HEARTH is a small Python MCP gateway that
   wraps a handful of local tools and logs each call via a `@ledgered` decorator to an append-only
   `ledger.jsonl` plus a queryable SQLite index, with SHA-256 digests of arguments and results. Its
   author is explicit about its scope: the digests are stored alongside the plaintext rather than
   independently verified (so the ledger is append-only but not cryptographically tamper-evident), it
   is single-user with no per-caller authorization, and it targets one workstation. That simplicity is
   the point of HEARTH, and it makes it the clearest minimal picture of the property we care about.
3. **Our audit boundary is deliberate (ADR-037).** We record only what the recorder itself executes
   and can prove against ground truth, and we keep client-side agent tool-calls out of scope on
   purpose: logging what a model *asked for*, without proving what actually happened, is a partial
   audit a caller can misrepresent, and a partial audit is worse than an honest boundary.
4. **Our ledger is already tamper-evident (ADR-058).** Append-only, with a per-row content hash, a
   server-set insert clock, and the raw payload retained by hash. That is the property HEARTH's design
   points at, which we have built.
5. **External direction is converging here.** Google's "Beyond Zero" enterprise-security paradigm
   (2026) authorizes every action at the resource level, at machine-speed, for humans and agents, and
   is explicitly MCP-aware. It authorizes at the tool-call boundary; an audit layer records and proves
   at the same boundary. The two are complementary.

## Decision

Expose the audit layer's tools and memory as a first-class **MCP server**, offered as an **additive**
endpoint (the existing OpenAI-compatible chat surface stays byte-for-byte unchanged), where **every
MCP tool invocation is a first-class, tamper-evident audit event**: an audit row and trace carrying
the caller, session, and trace identifiers, written through the ADR-058 append-only path, authorized
per tool by scoped OAuth2/OIDC (Keycloak), and isolated per tenant by row-level security.

The load-bearing principle is ADR-037 read forward: **routing an agent's tool calls through the MCP
backend pulls them into the provable audit boundary**, instead of trusting a client-side echo of what
the agent claims it did. The deliverable is not "a log of what a model asked for". It is a
tamper-evident record of what the recorder actually executed, provable against ground truth.

### Positioning relative to HEARTH

| Dimension | HEARTH (workstation gateway) | AuditTrace MCP entry-interface (service) |
|---|---|---|
| Ledger integrity | append-only; digests stored, not independently verified | tamper-evident: per-row content hash, server-set insert clock, raw artefact (ADR-058) |
| Identity and tenancy | single-user | multi-user via Keycloak, per-tenant row-level isolation, OIDC federation |
| Authorization | none today (a per-caller key is future work) | per-tool OAuth2/OIDC scopes; resource-and-action-level |
| Audit boundary | the client executes and logs | the recorder executes and proves (ADR-037) |
| Scale | one machine | multi-tenant service |

This is not a criticism of HEARTH. It is the same instinct (auditability and sovereignty of AI),
carried from a single-developer, consumer-hardware expression to a multi-user, regulated-grade one.

## Design (phased; high-level)

1. **Additive MCP endpoint.** Mount an MCP server on a new path using a standard MCP transport. The
   OpenAI-compatible chat endpoint is not modified; MCP is purely additive.
2. **Tools declared at the entry.** The MCP tool listing is the "manifest": capabilities are
   discoverable up front, and every invocation lands on the audit trail. Phase 1 exposes the
   read/recall surface; later phases add write/curation tools under operator-tier scopes.
3. **Authorize, execute, record, return.** Each tool call validates the bearer, enforces the
   per-tool scope (deny means no execution and no data), executes under the caller's isolated context,
   records a tamper-evident audit event, and returns the result. Authorization is evaluated on every
   call, at the level of the specific tool and resource.
4. **One trace.** An MCP call joins the same trace as any other request, so a reviewer can reconstruct
   which caller invoked which tool, with which arguments and result, and when, from the audit trail
   alone.
5. **Harness as front-end.** With reasoning through the chat endpoint and tools through MCP, an agent
   harness can use this system as its sovereign, audited backend. A "fully audited agent" is then
   reasoning through the audited model plus tools through MCP-through-the-backend.

## Consequences

- **Positive.** Any MCP-speaking agent can use the audited tool surface without giving up the audit;
  agent tool calls move from an unprovable client echo into the tamper-evident boundary; the interface
  aligns with where enterprise authorization is heading (resource-and-action-level, MCP-aware).
- **Cost.** A new protocol surface to maintain and secure; per-tool scope hygiene; the write surface
  (later phases) needs operator-tier controls.
- **Out of scope.** Client-side agent local tools (shell, file edits, web) remain outside the audit
  boundary unless routed through the MCP backend. Self-learning loops are out of scope, though the
  ledger property that would make them safe is worth naming: append-only with per-row hashing lets a
  loop consume its own history without being able to launder it.

## Phase 2 Track B — the tool-broker gateway (ratified 2026-08-23)

**Decision: broker, not federated.** Phase 2 extends the MCP entry-interface from "a server of our
own tools" into an **audit gateway that fronts arbitrary downstream tools** — ours, other MCP
servers, third-party tools — so that routing a tool call through AuditTrace makes it a first-class
tamper-evident audit event, regardless of whose tool it is. Two shapes were on the table:

- **Federated** — AuditTrace's manifest merely *points at* a downstream server (e.g. returns its URL
  or delegates the connection), and the caller talks to the downstream server directly from then on.
  Rejected. The moment the caller's traffic leaves AuditTrace's process, the audit trail degrades to
  "AuditTrace was told this call would happen" — a claim about intent, not a proof of execution. That
  is exactly the partial-audit failure mode ADR-037 (§Context.3, above) already named and rejected for
  client-side tool calls; federating would reintroduce it deliberately, in the one place (the gateway
  itself) where it is cheapest to avoid.
- **Broker (chosen).** AuditTrace stays in the request path for the entire call: it authorizes, sends
  the outbound request to the downstream server itself, receives the response itself, and records
  both ends. The operator quote that settled this: *"Broker, we do not break our own contract."* The
  "contract" is ADR-037's own claim — audit what we can prove against ground truth — and only the
  broker model keeps every routed call inside that provable boundary.

**The honesty boundary, restated precisely for Track B (governs the pitch — do not drift from this).**
AuditTrace audits what it **brokers** — proxies, executes the network hop itself, and can prove both
the outbound request and the inbound response happened. A tool call the agent makes **directly**,
without going through this gateway, stays **out of the boundary**, exactly as it did for Phase 1's own
tools. The precise claim is *"route the tools through the audited gateway → a tamper-evident, provable
record of every brokered call"* — never *"AuditTrace audits any tool the agent calls"*. The routing is
the price of the proof, not an implementation detail to gloss over.

**Provenance stays explicit at the row level.** A brokered call is recorded as TWO audit rows sharing
one ``interaction_id`` (one trace) — an outbound-request row (caller, session, trace, tool, args-digest)
written before the forward, and a result row (result-digest, status) written after, whether the
downstream call succeeded, failed, or timed out. Both are tagged ``provenance="brokered"`` with the
downstream server's identity, so a reviewer can always tell "AuditTrace executed this itself" (Phase 1
own-tool rows, ``provenance`` NULL) from "AuditTrace brokered this to downstream X" — the two are never
conflated in the schema, mirroring how they are never conflated in the pitch.

**Downstream registry is operator-configured, not auto-discovered.** Per the portability invariant, the
set of brokered servers is env-parameterized config (``AUDITTRACE_MCP_BROKER_SERVERS``) — an operator
adds an entry (URL + the scope that gates every tool on that server) before any of its tools are ever
listed or callable. No untrusted server is ever auto-registered; the honesty boundary would be
meaningless if a downstream server could add itself to the audited surface without the operator's
say-so.

Implementation: `src/audittrace/services/mcp_broker.py` (registry, namespacing, forward, both audit
records) + `src/audittrace/routes/mcp.py` (merged `tools/list` manifest, `tools/call` dispatch routing)
+ migration 021 (`tool_calls.provenance`/`phase`/`downstream_server`/`downstream_tool`/`args_digest`/
`result_digest`, all nullable and additive). Track A (write/curation tools over MCP) is sequenced after
Track B merges, to avoid a manifest/dispatch merge conflict on the same files.

## Status of implementation

The decision is accepted; implementation is phased. **Phase 1 shipped in code 2026-08-23**
(`src/audittrace/routes/mcp.py` + `src/audittrace/services/mcp_bridge.py`, spec
`2026-08-23-SPEC-mcp-entry-interface-phase1.md`): a `POST /mcp` streamable-HTTP JSON-RPC 2.0
endpoint exposes the six read/recall tools from `MEMORY_TOOL_REGISTRY`
(`recall_decisions`, `recall_skills`, `recall_recent_sessions`, `recall_semantic`,
`read_decision`, `read_skill`) — no write/curation tools, enforced at both the manifest and
the dispatch edge with no admin bypass. Every `tools/call` is authorized per-tool against the
caller's Keycloak scopes (identity token-derived via the same `require_user` dependency
`routes/chat.py` uses, binding the RLS `app.current_user_id` ContextVar), executed, and
recorded through the SAME tamper-evident `_persist_interaction` / `_flush_pending_tool_calls`
path the chat tool loop uses (ADR-037/058) — one `InteractionRecord` (`source="mcp"`) + one
`ToolCall` row per call. `/v1/chat/completions` is unchanged (OpenAPI drift gate green,
additive-only diff). Live E2E through a real MCP client against a deployed image is DEFERRED
to the next candidate deploy per the ADR-049 heavy gate — local unit + HTTP-level tests are
green (`tests/test_mcp_bridge.py`, `tests/test_mcp_routes.py`) but this has not yet been
independent-reviewer-certified or deploy-verified. Later phases add write/curation tools
(operator-tier scopes) and an additive endpoint that lets Anthropic-Messages-style harnesses
use this system as their audited backend.

**Phase 2 Track B (the broker, §"Phase 2 Track B" above) shipped in code 2026-08-23**
(`src/audittrace/services/mcp_broker.py`, `src/audittrace/routes/mcp.py` extended, migration 021,
spec `2026-08-23-SPEC-mcp-phase2-tool-broker-and-write-tools.md`): `tools/list` merges the
Phase 1 own-tool manifest with every enabled, scope-authorized, operator-configured downstream
server's tools (namespaced `broker:<server>:<tool>`); a brokered `tools/call` authorizes against
the downstream server's `required_scope` BEFORE any forward, writes a `provenance="brokered"`
request-phase `ToolCall` row, forwards the JSON-RPC call to the downstream server under the
caller's own identity, and writes a second `provenance="brokered"` result-phase row (success,
downstream error, or timeout — all three reach this write) — both rows sharing the one
`InteractionRecord`/trace the call produced. Phase 1's own-tool manifest and dispatch are
byte-unchanged. Local unit + HTTP-level tests are green (`tests/test_mcp_broker.py`,
`tests/test_mcp_broker_routes.py`) against a stub downstream MCP server; live E2E against a real
deployed downstream is DEFERRED to the next candidate deploy per the ADR-049 heavy gate, same as
Phase 1. Track A (write/curation tools over MCP) and Track C (Bruno collection coverage of the
full MCP surface) are sequenced after this merges.
