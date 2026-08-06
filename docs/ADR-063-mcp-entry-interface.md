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

## Status of implementation

The decision is accepted; implementation is phased and pending. Phase 1 exposes the read/recall tools
over MCP with per-tool scopes and a first-class audit event per call. Later phases add write/curation
tools and an additive endpoint that lets Anthropic-Messages-style harnesses use this system as their
audited backend.
