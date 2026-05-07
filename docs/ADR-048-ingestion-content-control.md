# ADR-048 — Ingestion content-control service

**Status:** Proposed
**Date:** 2026-05-07
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-027 (MinIO object storage), ADR-029 (audit trail
completeness), ADR-041 (product boundary — eight named dependencies),
ADR-043 (Vault as sole secret store), ADR-046 (async chat-completion
persistence — pattern reuse), ADR-047 (server-side embedding).

## Context

`/memory/upload` and `/memory/index` accept binary content from
authenticated users and pass it directly to in-process parsers
(`pymupdf` for PDFs, future paths for other formats). The
memory-server pod handling this content holds:

- a Postgres connection (RLS-bypass-capable for the audit and
  manifest tables),
- a MinIO client with credentials to the shared bucket,
- a ChromaDB token,
- a JWKS cache and access to issued user JWTs in flight,
- (under ADR-043) a Vault token with read access to the project's
  secret tree.

Any code execution achieved inside this pod via a parser exploit
therefore inherits the full credential set of the memory-server
identity. That is an unacceptable trust boundary for a service that
ingests externally-supplied bytes.

The class of risk is not theoretical:

- PDF parser CVEs (mupdf, pymupdf, poppler) land regularly. A
  2024-class exploit chain produces RCE from a malformed
  cross-reference table.
- Embedded JavaScript in PDF, embedded executables, and
  steganographic payloads have all been observed in the wild.
- Even non-malicious input can carry information the document
  author intended to remove (unflattened redactions — see the PDF
  ingestion gap inventory, item #8).

The current architecture has no boundary between "untrusted bytes"
and "the pod that holds every credential the system uses." Closing
that gap requires moving the content-handling boundary outside the
memory-server.

## Decision

Introduce a separate `content-control` service as the ninth named
dependency in ADR-041's dependency taxonomy. All `/memory/upload`
traffic terminates in a quarantine prefix that the memory-server
cannot read; the content-control service scans, classifies, and
(on a clean verdict) promotes the object into the indexable prefix.
The memory-server only ever indexes content that has been promoted.

Three properties are non-negotiable:

1. **The memory-server must never read pre-scanned bytes.**
   Enforced by MinIO bucket policy and the application role's IAM,
   not by application-level discipline.
2. **Every verdict must produce an audit record.** Both clean and
   rejected outcomes propagate to the existing `interactions`
   audit trail with a distinct `event_class` so SOC tooling can
   alert on rejections.
3. **The service interface must be implementation-agnostic.**
   AuditTrace-AI commits to the *interface*; operators choose the
   scanning backend (open-source default, commercial alternatives,
   or ICAP-fronted enterprise gateways).

## Architecture

```
┌─────────────┐
│   Client    │  PDF upload (authenticated, scoped JWT)
└──────┬──────┘
       │
       ▼
┌──────────────────────────────────┐
│  memory-server                    │
│  /memory/upload                   │
│  - validate JWT + memory:*:write  │
│  - file size pre-check            │
│  - PUT to MinIO quarantine prefix │
│  - emit manifest entry status:    │
│    "pending_scan"                 │
│  - return 202 + scan_id           │
└──────────────┬───────────────────┘
               │
               │ MinIO bucket prefix:
               │ s3://shared/quarantine/<user_id>/<uuid>/<filename>
               │
               │ ┌──── BUCKET POLICY ────────────────────────────┐
               │ │ audittrace_app role:                          │
               │ │   - PutObject on quarantine/* : ALLOW         │
               │ │   - GetObject on quarantine/* : DENY          │
               │ │   - GetObject on episodic/papers/* : ALLOW    │
               │ │ content_control role:                         │
               │ │   - GetObject on quarantine/* : ALLOW         │
               │ │   - PutObject on episodic/papers/* : ALLOW    │
               │ │   - DeleteObject on quarantine/* : ALLOW      │
               │ └───────────────────────────────────────────────┘
               │
               ▼
        S3 event notification
               │
               ▼
┌──────────────────────────────────┐
│  content-control service          │
│  (separate pod, separate identity)│
│                                   │
│  1. Read from quarantine          │
│  2. Pre-flight bounds check       │
│     (size, type, magic bytes)     │
│  3. Malware scan                  │
│     (ClamAV / ICAP / pluggable)   │
│  4. Content classification        │
│     (v2: PII, DLP, sensitivity)   │
│  5. Emit verdict                  │
│  6. Promote (clean) or            │
│     delete + audit (rejected)     │
└──────────────┬───────────────────┘
               │
               │ verdict via Redis Streams
               │ (audittrace:scan:verdicts)
               │ — reuses ADR-046 infrastructure
               │
               ▼
┌──────────────────────────────────┐
│  memory-server consumer           │
│  - update manifest entry:         │
│    status: scanned_clean / rejected│
│    scanner: <name@version>        │
│    signature_db: <hash>           │
│    scan_ts: <iso8601>             │
│    threat_name: <if rejected>     │
│  - emit audit record              │
│    event_class: SECURITY          │
│  - if clean: object now visible   │
│    in episodic/papers/* and       │
│    eligible for /memory/index     │
└──────────────────────────────────┘
```

## Why a separate service (not an in-process scanner library)

Five reasons, each independently sufficient:

1. **Trust boundary.** A scanner exploit in a separate pod, under
   a separate Kubernetes ServiceAccount, with a separate Vault
   path, with no Postgres / ChromaDB / JWT-issuing-key access, is
   a contained incident. The same exploit in-process is total
   credential compromise.

2. **Resource profile mismatch.** ClamAV's `freshclam` keeps a
   ~250 MiB signature database resident and updates it hourly.
   YARA rule sets are similar in scale. Co-locating that with the
   embedder caused an OOMKill once already (ADR-047 incident,
   2026-05-06); embedding the scanner would re-create the same
   class of problem.

3. **Update cadence mismatch.** Signature databases update hourly.
   Memory-server images update per-PR. Coupling them means either
   every memory-server release ships stale signatures, or every
   signature update triggers a full memory-server rollout.

4. **Implementation pluggability.** Operators have existing
   standards: ICAP gateways, commercial AV, sandboxed detonation
   services, in-house DLP. The dependency contract should accept
   any of them. An in-process Python library binds the project to
   one implementation forever.

5. **Defence in depth.** Even with a perfect scanner, the
   architecture should not assume the scanner is perfect. A bypass
   — zero-day, evasion technique, signature gap — should not result
   in credentialed code execution. Process isolation provides the
   second layer that the scanner alone cannot.

## Service interface contract

The content-control service is consumed through three documented
surfaces. All three are HTTP/JSON over Istio mTLS within the cluster.

**Surface 1 — Synchronous scan (RPC).**

```
POST /v1/scan
Authorization: Bearer <service-account JWT>
Content-Type: application/json

{
  "object_uri": "s3://shared/quarantine/<user>/<uuid>/<filename>",
  "expected_content_type": "application/pdf",
  "user_id": "<originating user>",
  "scan_modes": ["malware", "structure", "classification"]
}

→ 200 OK
{
  "verdict": "clean" | "rejected" | "scan_failed",
  "scanner": "clamav@1.3.1",
  "signature_db": "sha256:abc123...",
  "scan_duration_ms": 1247,
  "threats": [],          // populated if rejected
  "warnings": [],         // populated for non-fatal findings
  "classification": {     // optional, scan_modes-dependent
    "pii_detected": false,
    "sensitivity": "internal"
  }
}
```

Used for paths where the caller waits (small files, interactive
uploads).

**Surface 2 — Asynchronous scan (event-driven).**
S3 event notification triggers the scanner; verdict is published to
`audittrace:scan:verdicts` Redis Stream (reusing the ADR-046
consumer-group pattern). The memory-server consumer updates the
manifest and (on `clean`) promotes the object.

Used for the default `/memory/upload` path where the caller does not
block.

**Surface 3 — Health / readiness.**

```
GET /v1/health   → liveness
GET /v1/ready    → signature DB freshness, scanner subprocess health
GET /v1/version  → scanner version, signature DB hash, last-update timestamp
```

The `signature_db` hash from `/v1/version` propagates into every
scan verdict for reconstructibility.

## Failure modes and decisions

The scanner has four operational outcomes. Each requires a
documented response.

| Outcome | Response | Justification |
|---|---|---|
| `clean` | Promote object, manifest = `scanned_clean`, audit emission | Normal path |
| `rejected` (threat detected) | Delete from quarantine, manifest = `rejected_malware`, audit emission with `event_class: SECURITY`, **do not retry** | Retrying a known-bad object is itself an incident |
| `scan_failed` (scanner error) | Leave in quarantine, manifest = `scan_pending`, retry with exponential backoff up to N attempts, then `scan_unrecoverable` | Distinguishes transient from permanent failure |
| `scanner_unavailable` (scanner down) | New uploads return 503, in-flight quarantine entries hold | **Refuse-by-default is the only defensible audit posture** — a regulator will accept "we refused to ingest because the scanner was down" but will not accept "we ingested without scanning because the scanner was down" |

The fourth row is the most important and most counter-intuitive. The
temptation to "fail open" (allow uploads to proceed if the scanner
is unavailable) trades a short outage for a permanent gap in the
audit story. Refuse-by-default keeps the audit invariant intact.

## Audit trail integration

Every scan outcome produces an audit record with:

- The five reconstructibility identifiers from ADR-029: `user_id`,
  `session_id` (when applicable — uploads often have none),
  `interaction_id`, `trace_id`, `response_id`.
- A new `event_class` enum value: `SECURITY`. Distinguishes
  ingestion-control events from interaction events without breaking
  the existing audit schema.
- Scanner identity: `scanner_name`, `scanner_version`,
  `signature_db_hash`, `scan_modes` actually run.
- Verdict: `clean` / `rejected` / `scan_failed` /
  `scanner_unavailable`.
- For rejections: `threat_name`, `threat_family`, `confidence`.
- Object identity: `object_uri`, `object_sha256`,
  `object_size_bytes`, `original_filename`, `claimed_content_type`,
  `detected_content_type` (from libmagic — must be checked, must
  not be trusted from the upload header).

The `object_sha256` is computed at upload time, before quarantine
PUT, and is the canonical identity of the bytes for the entire
downstream lifecycle. Two uploads of the same file produce the same
SHA and can be deduplicated; the audit trail still records two
upload events but only one scan.

## Operator-facing surface

Three operator endpoints that the design must support from day one:

1. **`GET /v1/scan/status?object_uri=...`** — Returns the current
   state of any object in the system (`pending_scan`, `scanning`,
   `scanned_clean`, `rejected_malware`, `scan_unrecoverable`). Lets
   a user querying "what happened to my upload?" get a definite
   answer.

2. **`POST /v1/scan/retrigger`** (admin scope only) — Force a
   re-scan of an object. Used when the signature DB has been
   updated and an operator wants to re-check previously-clean
   objects against new signatures, or after a `scan_failed` to
   manually retry.

3. **`GET /v1/scan/stats?from=...&to=...`** — Aggregated counts by
   verdict, scanner version, threat family. Gives operators the
   data they need to answer "are we seeing more rejections lately?"
   without running ad-hoc queries against the audit trail.

## Implementation backends

The decision is the *interface*; the implementation is the
operator's choice. AuditTrace-AI ships with one default and
documents three alternatives.

**Default backend — ClamAV.**
Open source (GPLv2), self-hostable, signature database maintained
by Cisco Talos, well-understood operational profile, ICAP-compatible.
Sufficient as a baseline malware scanner. Ships as a sibling Helm
chart (analogous to the AiSovereignObservability sibling repo
pattern from ADR-021.2).

**Alternative — ICAP gateway.**
The Internet Content Adaptation Protocol (RFC 3507) is the standard
interface for enterprise content scanners. Symantec, Trend Micro,
McAfee, Cisco, Sophos all expose ICAP. An ICAP-speaking adapter
inside the content-control service lets operators plug in whatever
they already run.

**Alternative — Cloud-native scanners.**
For deployments with cloud egress allowed, Google Cloud DLP, AWS
Macie, Azure Defender for Storage, VirusTotal API. Each becomes a
different content-control service implementation.

**Alternative — Sandboxed detonation.**
For higher-tier deployments, dropping the file into a sandbox
(Cuckoo, CAPE, Joe Sandbox) and observing behaviour rather than
signature-matching. Slower, more expensive, more thorough. Same
interface, different backend.

## v1 / v2 scope split

**v1 — Malware scan only.**
- Single-backend (ClamAV) integration.
- Synchronous + async surfaces both implemented.
- All four outcome paths handled.
- Audit trail integration complete.
- Quarantine bucket policy enforced.
- Operator endpoints in place.

**v2 — Content classification.**
- PII detection (Microsoft Presidio or equivalent).
- DLP rules (configurable patterns).
- Sensitivity classification (PUBLIC / INTERNAL / RESTRICTED tagging).
- Same service, additional `scan_modes` values.
- Same verdict structure, additional `classification` fields.

**v3 — Behavioural / sandboxed analysis.**
- Pluggable detonation backend.
- Slow-path scan for high-risk content types.
- Runs in addition to v1 signature scan, not instead of.

The v1/v2/v3 split keeps the initial delivery small while ensuring
the interface accommodates the later additions without breaking
changes.

## Threat model — what this addresses, what it does not

**What this addresses:**
- Code execution from parser exploits in untrusted PDFs (and future
  formats).
- Known-malware content carried in user uploads.
- Insider risk where a user (or a user whose credentials are
  compromised) attempts to seed the corpus with malicious content.
- Audit-trail completeness for rejected uploads.

**What this does not address (and would need separate decisions):**
- A trusted-but-incompetent administrator who uploads sensitive
  content into the wrong project. That is an authorisation problem
  (ADR-026 RLS, scope discipline), not a content-control problem.
- An attacker who compromises the scanner itself. Mitigated by
  isolation (separate pod, separate identity) and reduced blast
  radius, not eliminated.
- Steganographic content that is benign to scanners but carries
  data exfiltration channels. Addressed in v3 or out of scope for
  AuditTrace-AI as a product.
- Adversarial inputs targeting the embedding model itself (prompt
  injection via document content). Out of scope for the
  content-control service; addressed at the agent / consumer layer.

## Acceptance criteria

The ADR is accepted when:

1. The Helm chart for the content-control service ships with
   sensible defaults and is deployable on a fresh k3s install via
   `make k8s-install`.
2. The MinIO bucket policy is enforced and tested — a unit test
   must verify that the `audittrace_app` role cannot read from the
   `quarantine/*` prefix even with a deliberately crafted query.
3. The four outcome paths each have an end-to-end test exercising
   the full upload → scan → verdict → manifest update cycle,
   including the `scanner_unavailable` refuse-by-default behaviour.
4. Audit trail emission is verified: every scan outcome produces an
   `interactions` row with `event_class: SECURITY` and the full set
   of fields from the *Audit trail integration* section.
5. The reconstructibility walkthrough is extended with a "rejected
   upload" scenario: given a rejection, an operator can reproduce
   the four-API-call drill to obtain the full evidence bundle (who
   uploaded, when, what file, which scanner, which signature DB
   version, what threat).

## Cross-references

- **ADR-027** — quarantine prefix is a new MinIO bucket structure;
  ADR-027's KMS encryption and bucket policy patterns extend
  cleanly.
- **ADR-029** — audit trail completeness extends to scan verdicts;
  the `event_class: SECURITY` is a schema addition, not a new
  table.
- **ADR-041** — content-control becomes the ninth named dependency;
  the dependency table in
  `docs/architecture/product-and-dependencies.md` updates
  accordingly.
- **ADR-043** — Vault holds the scanner's credentials, the ICAP
  endpoint URL (if external), and the signature-DB update
  credentials.
- **ADR-046** — async verdict propagation reuses the Redis Streams +
  consumer group + DLQ pattern; the `audittrace:scan:verdicts`
  stream is structurally identical to `audittrace:persist:stream`.
- **ADR-047** — establishes that resource-heavy components (the
  embedder) live behind a controlled interface; the scanner follows
  the same principle one level higher (separate process, not just
  separate function).
