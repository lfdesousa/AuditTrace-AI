# ADR-029: End-to-End Audit Trail — Project Tagging & HTTP Telemetry Refinements

**Status:** Accepted
**Date:** 2026-04-14
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-014.4 (OTel logging + tracing), ADR-025 (memory-as-tools),
ADR-027 (MinIO object storage), ADR-028 (observability aggregation stack)

## Context

Two gaps surfaced while validating the observability stack after the Tempo
service-graph was added in ADR-028:

1. **Audit rows land with `project = 'unknown'`**. The `chat.py` route reads
   the project name from `payload.get("project")` and falls back to the string
   `"unknown"` when absent. OpenCode — our primary agent — never sets this
   field, so every interaction, tool_call, and session persisted to Postgres
   carries `unknown`. The `recall_recent_sessions` tool filters by project;
   when the model asks *"what did we discuss in the last two sessions of
   AuditTrace-AI?"* it gets zero matches because nothing is actually tagged
   with `AuditTrace-AI`. Audit completeness fails at the entry point.

2. **MinIO does not appear on the Tempo service graph**. The opentelemetry
   Python contrib package at `0.62b0` ships a `urllib3` instrumentor whose
   HTTP client spans emit only `http.url` / `url.full` — no `server.address`
   or `net.peer.name`. Tempo's `metrics_generator_processor_service_graphs_peer_attributes`
   list depends on one of those keys to materialise a peer node; without it
   the minio edge is silently missing from the service map even though spans
   are flowing. The same gap exists in both the `http` and `http/dup`
   `OTEL_SEMCONV_STABILITY_OPT_IN` modes.

Both issues break the same invariant: every interaction and every outbound
call must be **attributable** (to a project) and **visible** (as a service
graph edge). Audit trail completeness is ADR-025's measurement prerequisite
and the story we tell the regulator under EU AI Act Article 12.

## Decision

Two decisions bundled as one ADR because they were discovered together and
share the same end state (a queryable, complete audit trail):

1. Tag every `/v1/chat/completions` request with a project derived from an
   explicit HTTP header, with documented precedence over body-level sources.
2. Back-fill `server.address` on urllib3 spans via an instrumentor
   `request_hook`, and keep the HTTP semantic-convention migration stable
   via `OTEL_SEMCONV_STABILITY_OPT_IN=http/dup` during the transition.

### §1. Project tagging contract

**Precedence** on `/v1/chat/completions` (first hit wins, strings trimmed of
whitespace, non-string values ignored):

| Tier | Source | Rationale |
|---|---|---|
| 1 | `X-Project` HTTP header | Preferred. Client-chosen, decoupled from the OpenAI JSON schema, trivial to configure per-project in an agent's provider settings. |
| 2 | `body.metadata.project` | OpenAI-compatible metadata dict. Supported so agents that already use the standard field don't need a custom header. |
| 3 | `body.project` | Legacy direct body field (backward compatibility for curl probes and older clients). |
| 4 | `"default"` | Explicit default instead of the previous misleading `"unknown"`. |

**Trust model.** The header is accepted at face value — any authenticated
caller can claim any project tag, same honesty model as the `User-Agent`
source detection. Per-project ACLs (scoping memory recall, billing) would
require cross-checking against a JWT claim; that is out of scope for this
ADR.

**Implementation** (`src/audittrace/routes/chat.py`):

```python
def _resolve_project(request: Request, payload: dict[str, Any]) -> str:
    header = request.headers.get("x-project")
    if isinstance(header, str) and header.strip():
        return header.strip()
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        meta_project = metadata.get("project")
        if isinstance(meta_project, str) and meta_project.strip():
            return meta_project.strip()
    body_project = payload.get("project")
    if isinstance(body_project, str) and body_project.strip():
        return body_project.strip()
    return "default"
```

Called once at the top of `chat_completions`; the resolved value is
written back into `payload["project"]` so every downstream site
(`_persist_interaction`, `_set_genai_request_attributes`, tool-mode ambient
context) sees the same tag without signature changes.

### §2. Client-side configuration

A stdlib-only helper script ships as
`scripts/configure-project.py`. The entry workflow is:

```bash
# Before starting a new project (or re-starting OpenCode for one)
scripts/configure-project.py AuditTrace-AI        # or any project name
scripts/configure-project.py --show               # current value per provider
scripts/configure-project.py Foo --launch         # exec opencode after writing
scripts/configure-project.py Foo --dry-run        # preview without writing
```

The script locates `~/.config/opencode/config.json` (overridable with
`--config`), walks every entry under `provider`, and sets
`options.headers["X-Project"]` to the supplied name. A timestamped backup
(`config.json.bak-YYYYMMDD_HHMMSS`) is written alongside, then the
config is updated atomically via `os.replace`.

The `@ai-sdk/openai-compatible` provider — the one OpenCode uses for our
memory-server — forwards `options.headers` on every request without any
extra plumbing. Continue, Roo Code, and any OpenAI-compatible agent with a
custom-headers config work the same way.

### §3. HTTP semantic-convention migration

`OTEL_SEMCONV_STABILITY_OPT_IN=http/dup` is set on the `memory-server` service
in `docker-compose.yml`. In `dup` mode the `opentelemetry-instrumentation-httpx`
and `opentelemetry-instrumentation-urllib3` packages emit **both** the legacy
HTTP attributes (`http.url`, `http.method`, `http.status_code`, `net.peer.name`)
and the stable ones (`url.full`, `http.request.method`,
`http.response.status_code`, `server.address`) on every client span. This
lets consumers migrate on their own timeline; drop to `http` (new-only) once
no downstream query still reads the legacy keys.

Tempo's peer attribute list was simplified accordingly in
`observability-stack/tempo/tempo.yml`:

```yaml
metrics_generator_processor_service_graphs_peer_attributes:
  - peer.service
  - server.address   # httpx + urllib3 — clean host-name labels
  - net.peer.name    # redis (DB semconv, independent migration track)
  - db.name          # postgres, via SQLAlchemy (→ 'audittrace')
```

The previous `http.url` entry was dropped — full-URL labels are replaced by
clean host-name labels (`chromadb`, `keycloak`, `minio`, `host.docker.internal`
for the host-pinned llama-server, `postgres` via `db.name`).

### §4. urllib3 `server.address` back-fill

The urllib3 contrib instrumentor at `0.62b0` has an upstream gap: **it does
not set `server.address` or `net.peer.name` on client spans** under either
semconv opt-in mode. Verified by running a `ConsoleSpanExporter` probe
inside the memory-server container — urllib3 spans carry `http.url` and
`url.full` but neither of the host-level attributes Tempo needs for the
service-graph edge.

Back-filled via a `request_hook` wired into `URLLib3Instrumentor` in
`src/audittrace/server.py::lifespan`:

```python
def _urllib3_set_server_address(span, _instance, request_info) -> None:
    if span is None or not span.is_recording():
        return
    try:
        parsed = urlparse(request_info.url)
        if parsed.hostname:
            span.set_attribute("server.address", parsed.hostname)
        if parsed.port:
            span.set_attribute("server.port", parsed.port)
    except Exception:
        pass  # hooks must never raise

URLLib3Instrumentor().instrument(
    tracer_provider=tp,
    request_hook=_urllib3_set_server_address,
)
```

The `opentelemetry-instrumentation-urllib3>=0.48b0` dependency was added to
`requirements.txt` and `pyproject.toml`. `MinIO` is the primary beneficiary
(the `minio` Python SDK goes through `urllib3.PoolManager`) but the hook
covers every future urllib3 consumer transparently.

### §5. Validation evidence

Probed live against the running stack on 2026-04-14:

```text
$ curl -s 'http://localhost:19090/api/v1/query?query=traces_service_graph_request_total' \
    | jq -r '.data.result[] | "\(.metric.client) -> \(.metric.server) [\(.metric.connection_type)]"'
user                      -> audittrace-server [virtual_node]
audittrace-server   -> chromadb                [virtual_node]
audittrace-server   -> redis                   [virtual_node]
audittrace-server   -> keycloak                [virtual_node]
audittrace-server   -> postgres                [database]
audittrace-server   -> host.docker.internal    [virtual_node]   # ← llama-server on host
audittrace-server   -> minio                   [virtual_node]   # ← NEW
```

All six outbound edges render with clean host-name labels. Project tagging
validated via three FastAPI TestClient integration tests asserting that
`interactions.project` carries the header value, the metadata fallback value,
and the default `"default"` as the contract dictates.

## Consequences

### Positive

- `recall_recent_sessions` becomes useful across projects. Once OpenCode is
  configured per-project, the memory-server can answer "what did we discuss
  in the last N sessions of AuditTrace-AI?" — one of the original
  memory-as-tools promises in ADR-025.
- The Tempo service-graph is complete end-to-end. MinIO was the last edge
  missing; we now have the full call chain visible for both audit and
  debugging.
- Project tagging precedence is documented and header-first, so adding a new
  agent is a single configuration-file change — no server-side code needed.
- `configure-project.py` is stdlib-only and distributable.

### Negative / caveats

- **`"default"` is still a sink.** Clients that don't configure a project tag
  still land in a catch-all bucket. Fixing that requires either making the
  header mandatory (breaks existing clients) or enforcing project identity
  through a JWT claim (ADR-026 follow-up).
- **`observability-stack/tempo/tempo.yml` is not tracked by git.** The
  simplified `peer_attributes` list lives outside the repo; the config is
  portability-flagged (same issue already noted for the gitleaks hooks).
  If that directory is wiped, the service-graph will lose MinIO and
  keycloak/chromadb labels will revert to defaults.
- **`http/dup` doubles attribute cardinality** on every HTTP client span.
  Storage and query cost is negligible at our volume but worth flagging; the
  migration path is to drop to `http` (new-only) once no dashboard or alert
  still queries `http.url` / `net.peer.name`.
- **urllib3 hook is a patch for an upstream gap.** When
  `opentelemetry-instrumentation-urllib3` ships a version that sets
  `server.address` natively, the hook becomes redundant and should be
  removed. Tracked as a low-priority follow-up.
- **Async persistence is still synchronous** and orthogonal to this ADR.
  The `_persist_interaction` + `_flush_pending_tool_calls` code path writes
  to Postgres on the request-handling thread; moving it onto a queue is a
  separate future ADR. This ADR makes the payload *correct* (tagged with the
  right project), which is a prerequisite for any queue-based handoff.

### Follow-ups

- Per-project ACLs via JWT claim cross-check (blocks claiming another
  project's tag). Depends on ADR-026.
- Track `opentelemetry-instrumentation-urllib3` for native `server.address`
  support and drop the `request_hook` when available.
- Bring `~/work/observability-stack` under version control.
- Async audit-row persistence (queue handoff) once an outbound message bus
  is available.
