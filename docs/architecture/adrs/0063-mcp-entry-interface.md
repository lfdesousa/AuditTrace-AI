# ADR-063 — MCP entry-interface: the audit layer as an MCP server

**Status:** Accepted (implementation phased; Phase 1 + Phase 2 Tracks A/B/C now merged to `main`)

Canonical decision record: [`docs/ADR-063-mcp-entry-interface.md`](../../ADR-063-mcp-entry-interface.md)
(this file is a naming-convention pointer only, kept for the Structurizr `!adrs` browser —
`NNNN-title.md`, four-digit — never a duplicate; edit the canonical file, not this one).

`POST /mcp` mounts a standard MCP transport (streamable-HTTP, JSON-RPC 2.0) alongside
`/v1/chat/completions`, purely additive. Every `tools/call` is authorize → execute/forward →
record → return, through the SAME tamper-evident audit path
(`_persist_interaction` / `_flush_pending_tool_calls`, ADR-037/058) the chat tool loop uses.
Phase 1 exposes the existing read/recall tools; Phase 2 Track A adds MCP-only write/curation
tools (`write_decision`/`write_skill`, per-tool scope, no admin bypass); Phase 2 Track B adds
the broker (`broker:<server>:<tool>` dispatch fronting operator-configured external MCP
servers, honesty boundary: AuditTrace audits what it actually brokers, never a client's
self-report of a direct call it never saw).
