# ADR-052 — PAdES trust store (pluggable Provider/Builder) and 8-class signature taxonomy

**Status:** Accepted
**Date:** 2026-05-09
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-043 (Vault as sole secret store — and what is *not* a secret), ADR-049 (Test, Evidence, Reconstructibility Gate), ADR-050 (PDF tier-B), [pdf-ingestion-gaps.md](architecture/pdf-ingestion-gaps.md), [pdf-ingestion-status.md](architecture/pdf-ingestion-status.md), [product-and-dependencies.md](architecture/product-and-dependencies.md), tier-A PR #42 (signature_status taxonomy origin), [backlog #13](backlog/13-pades-trust-store-provisioning.md) (closed by PR 3 of this ADR).

## Context

Tier-A PDF robustness (PR #42, 2026-05-07) shipped a per-chunk `signature_status` field with seven possible values (`check_skipped`, `check_unavailable`, `check_failed`, `none`, `signed_valid`, `signed_invalid`, `signed_tampered`). Tier-A also wired `_get_validation_context()` (`src/audittrace/routes/memory.py:428-466`) to accept an operator-provided PEM bundle path via the `pdf_signature_trust_store` setting.

Live-evidence capture on 2026-05-07 surfaced two distinct gaps:

1. **The deployed image has zero ZertES / eIDAS qualified-signature roots in its trust context.** `main_signed.pdf` (the canonical SwissSign-signed framework paper) flags `signed_invalid` ×46, even though the signature is structurally fine. The audit signal is honest given the empty trust context, but actively misleading in the audit log.
2. **`signed_invalid` collapses two distinct realities** at the `valid=False` / `trusted=False` checks (`memory.py:528-529`): "signature math is broken" (a real audit signal — the file was tampered or the signing operation failed) and "signing CA is not in our trust store" (a configuration gap on our side). An auditor who sees `signed_invalid` cannot tell which.

Backlog #13 was filed to provision the trust store via SwissSign roots. A pickup attempt on 2026-05-08 was deferred: the SwissSign download portal is reCAPTCHA-gated, the Mozilla CA Program (source for `certifi` and Debian `ca-certificates`) is scoped to WebPKI/TLS, and the EU eIDAS-program roots are distributed via different channels (Microsoft AATL, EU LOTL, Swiss federal TSL).

Investigation on 2026-05-09 (this ADR's pre-work) found that **pyhanko's `[etsi]` extra ships a maintained `lotl_to_registry()` function** (introduced in pyhanko 0.30.0; current 0.35.1 verified on PyPI 2026-05-08, MIT-licensed). It performs the EU LOTL fetch, XAdES verification, and per-member-state TSL walk in a single async call, and **bundles the EU LOTL bootstrap signing keys with the library**. We are already on `pyhanko>=0.21.0` for tier-A signature validation; bumping to `>=0.35.1` and enabling the `[etsi]` extra obviates the entire bootstrap-cert sourcing question.

Per the user's design push during planning: this is the v1 product surface for trust roots and **must be pluggable**. The trust store cannot be hardwired to either MinIO or to pyhanko[etsi]. A customer running an air-gapped deployment, a Vault-everything security posture, or an Adobe-AATL-aligned compliance regime needs to substitute providers and builders without re-architecting. The pluggability shape mirrors the existing `EpisodicService` ABC + `S3EpisodicService` + `MockEpisodicService` triad in `services/episodic.py`.

## Decision

This ADR records six decisions, delivered across three PRs that land in dependency order. The PR sequence is **PR 1 = this ADR (no code) → PR 2 = taxonomy split → PR 3 = pluggable trust-store implementation + admin endpoint + Helm hook**.

### #1 — Split `signed_invalid` into `signed_invalid` + `signed_untrusted`

**Decision.** PR 2 widens the signature taxonomy from 7 to 8 classes by adding `signed_untrusted`. The split is at the pyhanko status flags:

| Pyhanko outcome | Pre-PR-2 status | Post-PR-2 status |
|---|---|---|
| `intact=False` (any) | `signed_tampered` | `signed_tampered` (unchanged) |
| `intact=True, valid=False` | `signed_invalid` | `signed_invalid` (math broken — kept) |
| `intact=True, valid=True, trusted=False` | `signed_invalid` | **`signed_untrusted` (NEW)** |
| `intact=True, valid=True, trusted=True` | `signed_valid` | `signed_valid` (unchanged) |

**Why split rather than overload:**

1. `signed_invalid` and `signed_untrusted` are different audit categories. The first is a **security signal** ("someone tried something"); the second is a **configuration signal** ("we don't know this CA"). Collapsing them makes both meaningless — an auditor cannot use either to drive an action.
2. The split is post-hoc honest: tier-A's existing per-chunk metadata already carries the data needed to disambiguate; we just stop throwing it away.
3. Even with a fully-populated trust store (PR 3), customers will upload PDFs signed by CAs we don't carry. `signed_untrusted` remains a real, durable category — not a stop-gap.

**No migration required.** Verified at `src/audittrace/migrations/versions/010_add_pdf_manifest_columns.py:85` — `signature_status` is `sa.String(length=32), nullable=True` with no CHECK constraint and no enum. The new value drops in.

**Closed-set discipline.** A new test class `TestSignatureStatusCodes` in `tests/test_memory_routes.py` mirrors the existing `TestExtractionWarningCodes` pattern (set-equality assertion against the closed enum). A 9th class added without an ADR amendment fails CI.

### #2 — Trust store is pluggable behind a Provider + Builder ABC pair

**Decision.** PR 3 introduces two ABCs in a new `src/audittrace/services/trust_store.py`, mirroring the file shape of `services/episodic.py` (single file, ABC at the top, impls below).

```python
class TrustStoreProvider(ABC):
    """Where the PEM bundle lives. Pluggable: S3, Vault, File, ..."""
    async def load(self) -> TrustStoreBundle: ...
    async def store(self, bundle: TrustStoreBundle) -> None: ...
    async def metadata(self) -> TrustStoreMetadata: ...

class TrustStoreBuilder(ABC):
    """Where the PEM bundle comes from. Pluggable: EU LOTL, AATL, Static, ..."""
    async def build(self) -> TrustStoreBundle: ...
    @property
    def builder_id(self) -> str: ...  # carried in metadata for audit
```

`TrustStoreBundle = (pem_bytes: bytes, metadata: TrustStoreMetadata)`. `TrustStoreMetadata = (sha256, builder_id, built_at, cert_count, source_url)`.

**Why two ABCs not one:**

1. **Provider** answers "where does the PEM live?" (storage). **Builder** answers "where did the PEM come from?" (sourcing). These rotate independently — a customer may use `S3TrustStoreProvider` with an `AdobeAATLTrustStoreBuilder` (sourced from Adobe, stored in MinIO) without changing the storage strategy.
2. The Provider/Builder split mirrors the rest-of-codebase distinction between "service that retrieves" (`EpisodicService`) and "service that produces" (e.g. the embedder module's `ONNXEmbedder` vs the `Embedder` ABC). Keeping the same separation makes the cognitive load on a reviewer zero.

**Why ABC + impls in one file (`services/trust_store.py`) and not a package:** `services/episodic.py` is 371 lines with an ABC + S3 impl + Mock impl all colocated. The trust-store module will land in a similar shape (~400-500 lines for two ABCs + four impls + two dataclasses). Mirroring an in-repo pattern is more valuable than a different organisation that is theoretically tidier.

### #3 — PR 3 ships two Provider impls and two Builder impls

**Decision.** The ABCs in PR 3 are accompanied by exactly four implementations:

| ABC | Default | Test/dev | Future (ABC contract supports; not implemented) |
|---|---|---|---|
| `TrustStoreProvider` | `S3TrustStoreProvider` (MinIO via existing `services/episodic.py` S3 client factory) | `MockTrustStoreProvider` (in-memory, follows existing `MockEpisodicService` shape) | `VaultTrustStoreProvider`, `ConfigMapTrustStoreProvider` |
| `TrustStoreBuilder` | `EuLotlTrustStoreBuilder` (calls `pyhanko.sign.validation.lotl_to_registry(lotl_xml=None)`, filters to qualified-signature service types, exports PEM) | `StaticTrustStoreBuilder` (concatenates a list of operator-supplied PEMs from a configured directory) | `AdobeAATLTrustStoreBuilder`, `CefDssSidecarBuilder` |

**Why two implementations per ABC, not one:**

1. **One impl is no abstraction.** A single concrete impl with an ABC over it is unjustified ceremony. Two impls force the ABC contract to be honest — anything `S3TrustStoreProvider` does that `MockTrustStoreProvider` cannot is a leaky abstraction caught at PR-3 review.
2. `StaticTrustStoreBuilder` is not throw-away. It serves two real audiences: tests (predictable, no network), and air-gapped customers (operator vendors a PEM directory; no LOTL access required). The latter is a likely customer ask in regulated/government contexts.

**Why the future impls are spec'd but not implemented:**

- `VaultTrustStoreProvider`: see decision #4. Public CAs are not secrets; Vault is the wrong abstraction for the v1 default. ABC supports it for the customer who insists.
- `AdobeAATLTrustStoreBuilder`: useful for "what Acrobat trusts" parity. No customer has asked.
- `CefDssSidecarBuilder`: useful as a fallback if pyhanko[etsi]'s registry shape proves insufficient for some compliance posture. Premature; we ship pyhanko[etsi] and learn.
- `ConfigMapTrustStoreProvider`: alternative provisioning shape for Helm-only customers. `S3TrustStoreProvider` covers the same need today.

### #4 — MinIO is the default storage; Vault is a documented future impl

**Decision.** `S3TrustStoreProvider` is the default v1. The PEM bundle lives at `memory-shared/trust-store/eu-lotl-bundle.pem` in MinIO, alongside the existing memory-shared artefacts (ADR markdown, skill files, attachment quarantine). `VaultTrustStoreProvider` is documented in this ADR but **not implemented in PR 3**.

**Why MinIO over Vault:**

1. **EU LOTL roots are public CAs.** They are published on a public website with PR processes for additions and removals. Storing them in a secret manager — whose audit model is "who read this secret" — is conceptually muddled. Vault is for things that hurt if leaked (passwords, signing keys, API keys); the trust store is the opposite of that.
2. **MinIO is already the canonical storage layer** for AuditTrace-AI-managed artefacts (per `product-and-dependencies.md` §4 — Object Storage). ADR markdown, skill files, attachment quarantine, document indexing all live there. The trust store fits the same `memory-shared/<topic>/` prefix pattern; reviewers will recognise the shape.
3. **Versioning + rollback** is native to MinIO via S3 object versioning. Vault KV-v2 also supports versioning, but at smaller per-secret size limits.
4. **Size headroom.** The EU TSL union of qualified-signature CAs is roughly 400-800 KB. MinIO does not care about size; Vault's KV typically caps around 1 MiB per secret.

**Why Vault is in the ABC contract anyway:** A customer whose security team mandates "everything goes through Vault" gets `VaultTrustStoreProvider` as a 1-class addition, not a re-architecture. The ABC commits to this interface today; the impl waits for the ask.

**Per ADR-043 §"What is *not* a secret":** trust roots are not secrets. ADR-043's Vault-as-sole-secret-store discipline applies to credentials and signing keys, not to publicly-published trust lists.

### #5 — Refresh is operator-explicit (admin endpoint) plus Helm post-install hook

**Decision.** PR 3 introduces:

- A `POST /admin/trust-store/refresh` endpoint (scope `audittrace:admin`). Synchronous: calls `Builder.build()` then `Provider.store(bundle)`, invalidates the in-process `_VALIDATION_CONTEXT` singleton, returns `TrustStoreMetadata` JSON.
- A Helm post-install / post-upgrade hook Job (`charts/audittrace/templates/admin/job-trust-store-refresh.yaml`) that hits the admin endpoint once after deploy. Mirrors the existing pattern at `charts/audittrace/templates/postgres/job-summariser-role.yaml` (helm.sh/hook: post-install,post-upgrade; backoffLimit: 12; activeDeadlineSeconds: 600).

**Why explicit-refresh-only and not a background scheduler:**

1. **Refresh cadence ≈ release cadence is honest for v1.** The EU LOTL signing keys rotate roughly every 5 years; member-state TSPs are added/removed quarterly per state; CA-set composition is stable on the timescales that matter for an audit-grade product. A weekly background refresh is overkill at v1.
2. **No new external runtime dependency.** A background scheduler would force EU LOTL into the runtime dependency matrix in `product-and-dependencies.md` (it would become §9 — "EU Commission List of Trusted Lists"). With explicit refresh, EU LOTL is only hit during operator-initiated refresh — the dependency is **transient**, not standing.
3. **No new k8s objects.** A scheduler shape (CronJob, Argo Workflows, etc.) would add a new pod, a PVC, replica coordination, and failure-mode complexity. A simple admin endpoint plus a post-install hook is a delta of one route + one chart template — within the project's stated "one process, one image, one chart" stance (`product-and-dependencies.md`).
4. **Reviewable refresh.** When the operator hits the admin endpoint, the response carries the `TrustStoreMetadata` (sha256, cert_count, built_at, source_url). The next deploy's logs show the Helm-hook Job's metadata. Both are auditable in Tempo / Loki without bespoke instrumentation.

**Why the Helm post-install hook (not just the endpoint):**

1. **First-deploy ergonomics.** Without a hook, the operator must remember to hit the admin endpoint after every install/upgrade, otherwise signature_status remains `signed_untrusted` until they do. Mistakable.
2. **Customer can opt out.** `values.yaml` exposes `trustStore.bootstrap.enabled: true` (default true). Air-gapped customers who use the `StaticTrustStoreBuilder` set `trustStore.bootstrap.enabled: false` and hit the endpoint themselves with whatever sourcing they prefer.

**A periodic refresher is reserved for ADR-053.** If a customer asks for sub-release-cadence refresh — for instance because they need to react to a CA revocation faster than our release cadence allows — we open ADR-053 for a `PeriodicTrustStoreRefresher` background asyncio task. That task would mirror `services/session_summarizer.py` (asyncio bg task, Postgres advisory lock for cross-replica coordination, telemetry per refresh attempt). Architecture sketch is in this ADR's "Out of scope" section so a future maintainer does not need to re-derive it.

### #6 — `pyhanko[etsi]` is a runtime dep, ImportError-guarded at the call site

**Decision.** PR 3 bumps `pyhanko>=0.21.0` to `pyhanko[etsi]>=0.35.1` in `pyproject.toml` and runs `make sync-requirements` to regenerate `requirements.txt` (drift-guard from PR #43 enforces consistency). The `EuLotlTrustStoreBuilder` imports the etsi machinery inside `build()` and catches `ImportError` to fall through to a typed error result, **not a silent failure**.

**Why runtime and not build-time-only:**

1. The default install path runs `EuLotlTrustStoreBuilder` in-process (admin endpoint and Helm hook both invoke it). Treating the etsi extra as "build-time only" would push the LOTL walk to a maintainer's laptop, which contradicts the pluggability decision (#2) and the operator-explicit refresh decision (#5).
2. An air-gapped customer using `StaticTrustStoreBuilder` *does not need* the etsi extra at runtime. The ImportError-guard means installing `pyhanko` without `[etsi]` is a valid deployment shape for that customer.

**Why ImportError-guarded:** any deployment that installs `pyhanko` without the `[etsi]` extra (`pip install pyhanko` instead of `pip install pyhanko[etsi]`) gets a working memory-server with `EuLotlTrustStoreBuilder.build()` raising a typed `TrustStoreBuilderUnavailableError("pyhanko[etsi] extra not installed")` — surfaced as a 503 from the admin endpoint, not a startup crash. PYTHON-ENGINEERING §4 (graceful-degradation pattern, mirrored from `_pdf_signature_status`'s existing `try: from pyhanko... except ImportError: return ('check_unavailable', 0)` shape).

## Consequences

### Positive

- **Audit clarity.** `signed_untrusted` removes the "is this a configuration gap or a tampering signal?" ambiguity from `signed_invalid`. Auditors get a clean four-way split: tampered, invalid (math), untrusted (CA scope), valid.
- **Closed-set discipline.** `TestSignatureStatusCodes` makes the taxonomy a contract — no silent additions.
- **Pluggability.** Customers with non-default storage (Vault) or non-default sourcing (Adobe AATL, CEF DSS) can substitute one ABC at a time without re-architecting.
- **No new runtime external dependency.** `product-and-dependencies.md` stays at 8 dependencies. EU LOTL is transient (only hit during refresh).
- **No new k8s objects.** The chart gains one Job template (mirroring the existing summariser-role and memory-scopes Jobs); no CronJob, no PVC, no new pod.
- **Backlog #13 closed.** The original "vendor SwissSign roots out-of-band" framing is superseded by "walk the EU LOTL programmatically" — a strict superset (Layer 1 SwissSign + Layer 2 EU eIDAS in one code path).

### Negative

- **Refresh latency = release cadence.** A customer who needs to react to a CA revocation faster than our release cycle allows is constrained, until ADR-053 ships a periodic refresher. Mitigated by the admin endpoint (operator can refresh manually at any time).
- **`pyhanko[etsi]` runtime size.** The etsi extra adds `httpx` and adjacent deps to the runtime image. Image size delta is small (~5-10 MB) but not zero. ImportError-guard means it's optional, but the default install carries it.
- **Vault customers without v1 support.** A customer who insists on Vault-only storage in v1 has to wait for `VaultTrustStoreProvider` (no ETA — built when asked).

### Risks

- **EU LOTL endpoint outage during refresh.** `EuLotlTrustStoreBuilder.build()` will raise on network failure; `Provider.store()` is not called; the existing `Provider.load()` continues to return the previously-cached bundle. Refresh is a no-op on outage; the admin endpoint returns 502 with the underlying error. Operator can retry. **Acceptable** — refresh is best-effort by design.
- **EU LOTL XAdES verification failure.** pyhanko surfaces this as an exception; same handling as above. The bundled bootstrap signing keys mean we don't have a chain-of-trust gap on our side; the only failure path is upstream LOTL state.
- **EU TSL bundle bumps against MinIO object size.** Unlikely (estimate 400-800 KB) but worth measuring on first refresh. If exceeded, MinIO multipart upload handles it transparently.
- **Pluggability premature.** Two impls per ABC may prove to be the only impls ever shipped. The cost of one extra impl-per-ABC at PR-3 review is low; the cost of retrofitting an ABC after the fact (when a customer asks for Vault) is high. **Net-positive bet.**
- **Per the user's "no shortcuts" feedback memory:** if PR 3's E2E live-evidence loop hits friction, the temptation is to skip the Helm hook or skip the admin endpoint and ship a chart-vendored static PEM. Resist. The pluggability is the v1 product surface; cutting it ships a v0.5 the next ADR has to undo.

## Validation per the gate (ADR-049 §Decision)

This ADR is the design artefact for three PRs. The gate applies to PRs 2 and 3 (PR 1 is a docs-only ADR; the file diff is the artefact).

| PR | Verification | Validation | Reconstruction |
|---|---|---|---|
| **PR 1** (this file) | n/a (docs-only) | n/a | n/a |
| **PR 2** (taxonomy split) | `pytest tests/test_memory_routes.py::TestSignatureStatusCodes -v`; full `make test` green; ≥90 % per-file coverage maintained on `routes/memory.py` | E2E re-index of `main_signed.pdf` against the deployed image (post-PR-2, pre-PR-3): `signature_status="signed_untrusted"` ×46. Honest intermediate state — trust store is still empty. | `psql` row, ChromaDB chunk metadata, Tempo trace ID; captured under `~/work/audittrace-evidence/2026-05-09-pr2-taxonomy/`. |
| **PR 3** (pluggable trust store) | `pytest tests/test_trust_store.py -v` (Provider + Builder contract tests with vendored fixture LOTL XML); `pytest tests/test_admin_routes.py::test_trust_store_refresh -v`; full `make test` green | E2E three flips: (a) `main_signed.pdf` → `signed_valid` ×46 after Helm-hook refresh ran; (b) tampered copy → `signed_tampered`; (c) self-signed PDF with non-EU-TSL CA → `signed_untrusted`. | For each flip: API response, `psql` row, ChromaDB metadata, Tempo trace ID, MinIO listing showing the PEM at `memory-shared/trust-store/eu-lotl-bundle.pem`; captured under `~/work/audittrace-evidence/2026-05-09-pr3-trust-store/INDEX.md`. |

## Out of scope (deferred to future ADRs)

- **Vault as trust-store provider** — `VaultTrustStoreProvider`. ABC contract supports it; no impl until a customer asks.
- **Periodic background refresher** — `PeriodicTrustStoreRefresher` (mirror of `SessionSummarizer`). Reserved for ADR-053 if sub-release-cadence refresh becomes a customer requirement. Sketch: asyncio bg task in memory-server lifespan; Postgres advisory lock for cross-replica coordination; telemetry span per refresh attempt; failure mode = log + retry on next interval; refresh interval as a setting (default 7 days).
- **Adobe AATL trust-list builder** — `AdobeAATLTrustStoreBuilder`. ABC supports it; no impl until a customer asks.
- **CEF DSS Java sidecar builder** — `CefDssSidecarBuilder`. Documented as a fallback if pyhanko[etsi]'s output proves insufficient for some compliance posture (e.g. a regulator who insists on the EU's reference Java implementation as the source of truth).
- **LTV (Long-Term Validation) metadata** — gap-inventory item #13. Backlog #13 retains it; this ADR closes the trust-store half but not the LTV half.
- **`signed_expired` as a 9th class** — pyhanko surfaces signing-time expiry as `valid=False`, lumping it into `signed_invalid` under the post-PR-2 taxonomy. Splitting it out is a low-leverage refinement; deferrable until an audit request specifically calls for it.
- **ConfigMap-baked-in static PEM** — alternative provisioning pattern. `StaticTrustStoreBuilder` covers the same need; full ConfigMap-bake-in is unnecessary unless a customer asks.

## Update protocol for the status doc

When PR 2 lands: edit [`docs/architecture/pdf-ingestion-status.md`](architecture/pdf-ingestion-status.md) item #12 row to reflect the 8-class taxonomy (taxonomy is the data signal; status remains `Code shipped, data gap` until PR 3 lands).

When PR 3 lands: same row → ✅ **Shipped**; tier-A summary updated to "code-complete; data-side resolved 2026-05-09 (this ADR + PR 3)"; backlog #13 closed with a "Resolved 2026-05-09 in ADR-052 / PR 3" stanza at the top of the backlog file.

The pre-commit gate does not enforce status-doc updates, but the test-and-evidence discipline (ADR-049) does — the PR body's Reconstruction section references this file's diff alongside the live-evidence artefacts.
