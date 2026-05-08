# ADR-053 — Swiss federal Trusted List + composite trust-store builder

**Status:** Accepted
**Date:** 2026-05-09
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-052 (PAdES trust store + 8-class signature taxonomy — pluggable Provider/Builder ABCs that this ADR extends), ADR-049 (Test, Evidence, Reconstructibility Gate), ADR-043 (Vault as sole secret store — *and what is not a secret*).

## Context

ADR-052 PR shipped `EuLotlTrustStoreBuilder` (the EU List of Trusted Lists walker via pyhanko[etsi]) as the v1 default sourcing layer. Live evidence on 2026-05-09 captured the headline flip — `Luis_Research_Proposal_signed.pdf` (signed by an EU-recognised qualified TSP) flipped from `signed_untrusted` to `signed_valid` end-to-end through the public API after the EU LOTL refresh.

But the same evidence run also exposed the residual scope gap: `main_signed.pdf` is signed by `SwissSign Signature Services Root 2020-2`. **Switzerland is not an EU member state**, so the EU LOTL does not by default reference the Swiss federal TSL. The 887 EU qualified-signature CAs in our trust store after the EU LOTL walk did not include the Swiss roots. `main_signed.pdf` honestly flagged `signed_untrusted` post-PR-3 — the audit signal was scope-aware and correct (configuration gap, not security signal), but the gap itself still needed closing.

For an audit-grade product running on Swiss soil, with Swiss customers, **Swiss-jurisdiction qualified TSPs in scope is a v1 baseline** — not a stretch goal. AuditTrace-AI is registered in Switzerland and the canonical demo PDF is Swiss-signed; "we don't trust SwissSign" is not a defensible product posture.

This ADR closes the gap by:
1. Adding a `SwissTslTrustStoreBuilder` that walks the Swiss federal Trusted List published by OFCOM (Federal Office of Communications) via pyhanko's per-TSL parser.
2. Adding a `CompositeTrustStoreBuilder` so an operator can run multiple jurisdictional builders in one refresh cycle — `[eu_lotl, swiss_tsl]` is the v1 default.
3. Vendoring the Swiss TSLO (Trust List Operator) signing certificate in the chart, OOB-verified against the published SHA-1 fingerprint.

The work fits inside ADR-052's Provider/Builder abstraction without modification — exactly the kind of one-class-per-ABC extension the ABC contract was designed for.

## Decision

This ADR records four decisions, all delivered in a single PR (`feat/swiss-tsl-trust-store`).

### #1 — `SwissTslTrustStoreBuilder` calls pyhanko's per-TSL parser

**Decision.** A new class `SwissTslTrustStoreBuilder` in `src/audittrace/services/trust_store.py`, mirroring the file shape of `EuLotlTrustStoreBuilder`. `builder_id = "swiss_tsl"`. `source_url = "https://trustedlist.tsl-switzerland.ch/tsl-ch.xml"`.

`build()` does:

1. Read the OOB-vendored TSLO cert from `Settings.pdf_trust_store_swiss_tslo_cert_path` (filesystem path — a ConfigMap-mounted file in production).
2. Parse it via `asn1crypto.x509.Certificate.load(der_bytes)`.
3. Open an `aiohttp.ClientSession` and GET the Swiss TSL XML from the published endpoint.
4. Call `pyhanko.sign.validation.qualified.eutl_parse.trust_list_to_registry(tl_xml, tlso_certs=[parsed_cert])`. pyhanko validates the TSL's XAdES signature against the supplied TSLO cert before adding any TSP to the registry — this is the chain-of-trust anchor.
5. Walk the resulting `TSPRegistry.known_certificate_authorities` via the existing `_registry_to_pem_bundle` helper (added in ADR-052 PR 3) and emit a PEM bundle.

**Why pyhanko's per-TSL parser, not a custom XAdES walker:**

1. **Same library, same trust contract.** pyhanko already validates the EU LOTL via the same parsing path; using it for the Swiss TSL means one library to audit, one quirk-set to track. ETSI TS 119 612 is the standard both lists conform to.
2. **No new dep.** `pyhanko[etsi,async-http]` is already in `pyproject.toml` from ADR-052. The Swiss-TSL builder ships at the cost of ~50 lines of glue.
3. **The contract is testable.** `trust_list_to_registry` raises `SignatureValidationError` if the TSL XAdES signature doesn't validate against the TSLO cert — i.e. either the TSL was tampered with, or the operator vendored the wrong TSLO cert. Both cases surface as `TrustStoreBuilderUnavailableError` from our wrapper, returning HTTP 502 from the admin endpoint.

### #2 — `CompositeTrustStoreBuilder` chains inner builders

**Decision.** A new class `CompositeTrustStoreBuilder(inner: list[TrustStoreBuilder])`. `builder_id` is the chain of inner builder IDs joined by `+` (e.g. `eu_lotl+swiss_tsl`). `build()` runs each inner builder in sequence and concatenates the resulting PEM bytes into one bundle.

**Failure mode (best-effort):**

- If ANY inner builder raises `TrustStoreBuilderUnavailableError`, the composite logs the error and continues with the remaining builders. The bundle reflects whichever builders succeeded.
- The composite raises `TrustStoreBuilderUnavailableError` only if EVERY inner builder fails.
- Auditors see `cert_count` per inner builder via the per-builder log lines, plus the composite `builder_id` chain in the bundle metadata.

**Why best-effort over fail-fast:**

A transient outage at `https://ec.europa.eu/tools/lotl/eu-lotl.xml` should not zero out our Swiss roots, and vice versa. The audit signal stays honest — the operator can read the metadata and notice that `cert_count` dropped from ~887+nnn to nnn alone, indicating the EU LOTL walker didn't run this cycle. Treating any one outage as a hard fail would invert the customer experience: a Swiss-signed PDF would flip from `signed_valid` to `signed_untrusted` because Brussels was offline. Best-effort means the system stays useful through partial outages.

The previous-bundle-survives invariant (per ADR-052 §5) still holds: even if every inner fails, `Provider.store()` is never called, and the existing cached bundle stays in MinIO until a successful refresh replaces it.

### #3 — Default composition: `eu_lotl,swiss_tsl`

**Decision.** Set `pdf_trust_store_builder` default to `"composite"` and `pdf_trust_store_composite_builders` default to `"eu_lotl,swiss_tsl"`. v1 chart deploys ship with EU + CH coverage out of the box.

**Why this is the v1 default, not an opt-in:**

1. **AuditTrace-AI's product surface.** The product is registered + operated in Switzerland. The canonical signed-PDF use cases (legal, regulatory, qualified e-signature) are CH-jurisdictional more often than EU-jurisdictional in the customer pipeline. A Swiss-signed PDF flagging `signed_untrusted` on first install is a customer-onboarding bug, not a feature.
2. **Cost is minimal.** The Swiss TSL fetch adds ~5-10 s to the refresh cycle (one HTTPS GET + one XAdES verification + one TSP walk). The EU LOTL walk takes ~22 s on its own; running both is still under 35 s — well inside the admin endpoint's timeout budget. The bundle size grows by ~20-50 KB for the Swiss TSPs — negligible.
3. **Honest scope.** A customer who deliberately wants EU-only or Swiss-only coverage flips `pdf_trust_store_composite_builders` to one entry. The default reflects what most deployments will want; the override reflects what some will.

### #4 — TSLO cert vendored in the chart, OOB-verified at vendoring time

**Decision.** The Swiss TSLO signing certificate (DER, ~2 KB) is vendored at `charts/audittrace/trust-store/swiss-federal-tsl/CH-TL-cert.der`. The chart renders a ConfigMap (`{Release}-swiss-federal-tsl`) from the file via `.Files.Get` + `b64enc`, mounted into memory-server at `/etc/audittrace/swiss-federal-tsl/`. `Settings.pdf_trust_store_swiss_tslo_cert_path` defaults to `/etc/audittrace/swiss-federal-tsl/CH-TL-cert.der`.

**Sourcing + OOB verification (2026-05-09):**

| Field | Value |
|---|---|
| Source URL (DER) | https://trustedlist.tsl-switzerland.ch/tsl-signer-certificate/CH-TL-cert-DER.cer |
| Source URL (Base64) | https://trustedlist.tsl-switzerland.ch/tsl-signer-certificate/CH-TL-cert-B64.cer |
| Documentation | https://uri.tsl-switzerland.ch/TrstSvc/TrustedList/schemerules/CH/index.html |
| Published SHA-1 | `e8 63 83 62 51 30 bd f0 1e 42 a3 17 65 01 e0 79 26 1b 13 7f` |
| Vendored SHA-1 | `e86383625130bdf01e42a3176501e079261b137f` ✓ matches |
| Vendored SHA-256 | `37c2b85994713a95fdfd6387747f4e4f1bcc50d9c99c7b1331ae41392ee3059e` |
| File size | 2 097 bytes |

**Why ConfigMap not Secret, not Vault:**

- The TSLO cert is **public** — it's published on the OFCOM-managed website. Storing a public artefact in a secret manager is conceptually wrong (the threat model for secrets is "leak hurts"; public certs do not hurt if leaked).
- ConfigMap binaryData is the right k8s primitive for a small public binary (≤1 MiB). 2 KB is well within scope.
- ADR-043's "what is *not* a secret" stance applies directly — same reasoning that put the EU LOTL bundle in MinIO instead of Vault.

**Refresh cadence for the TSLO cert itself:**

The TSLO cert rotates infrequently (Swiss federal signing keys are stable on multi-year horizons). Rotation is a maintainer task: update the file under `charts/audittrace/trust-store/swiss-federal-tsl/`, OOB-verify the new SHA-1, ship via the next chart release. No runtime fetch — runtime fetch of the bootstrap cert would defeat the OOB chain of trust.

If OFCOM rotates the TSLO cert and an operator's chart is stale, `SwissTslTrustStoreBuilder.build()` raises `TrustStoreBuilderUnavailableError("trust_list_to_registry failed: SignatureValidationError")` — the existing cached bundle in MinIO continues to be used by the validator, and the operator sees a clear refresh-time error.

## Consequences

### Positive

- **Swiss-signed PDFs flip to `signed_valid`** end-to-end via the admin refresh, including `main_signed.pdf` and any document signed by a SwissSign / Swisscom Trust Services / other ZertES-supervised qualified TSP.
- **EU + CH in one default install.** The composite default delivers two-jurisdiction coverage out of the box.
- **Pluggable extension.** Future jurisdictions (UK TSL, Norway/Iceland/Liechtenstein TSLs, country-specific operator additions) are one-class-per-ABC additions; the composite already accepts them.
- **Backlog #13 fully closed.** The original "vendor SwissSign roots" framing is now superseded by "walk the Swiss federal TSL programmatically," which is a strict superset of what backlog #13 asked for.
- **No new external runtime dependency in the §1.1 sense.** The Swiss federal TSL is hit only during operator-explicit refresh, transient like the EU LOTL — `product-and-dependencies.md` stays at 8 dependencies.

### Negative

- **Chart now contains a binary file** (`CH-TL-cert.der`). Reviewers see a 2 KB DER cert in `git status`; intentional + documented + OOB-verified, but does shift the chart from "all text" to "small binary blob." Mitigated by the ConfigMap binaryData pattern and the SHA-1 + SHA-256 in this ADR.
- **Refresh latency grows.** EU LOTL alone was ~22 s; with the Swiss TSL it's ~30 s. Inside the budget, but the admin endpoint's wall-clock is now bigger.
- **TSLO cert rotation is a maintainer task.** A future OFCOM rotation requires a chart bump. Documented but operationally manual until ADR-054 (if a customer asks for automatic TSLO refresh, which would require its own OOB chain).

### Risks

- **OFCOM endpoint unreachable at refresh time.** Best-effort composite handles this — the cached bundle survives, the admin endpoint logs the inner failure, the EU LOTL contribution still applies.
- **Swiss TSL XML schema drift.** ETSI TS 119 612 is stable but versions exist (v2.x in current use). pyhanko's parser tracks the standard; a schema change would surface as a `TSPServiceParsingError` per service, logged + continued like the EU LOTL's existing per-service warnings.
- **Composite ordering surprises.** If `eu_lotl` and `swiss_tsl` both list the same CA (theoretically possible if the EU LOTL ever cross-references CH via the EEA mechanism), pyhanko_certvalidator handles duplicate trust roots gracefully (set semantics on subject + key). No concrete known overlap today.

## Validation per the gate (ADR-049 §Decision)

| Verification | Validation | Reconstruction |
|---|---|---|
| `make test` green; `make lint` + `make format` clean; per-file ≥90% coverage on `services/trust_store.py` (extended with two new classes + tests). New tests: `TestSwissTslTrustStoreBuilder` (3 cases — id, missing cert, unparseable cert), `TestCompositeTrustStoreBuilder` (5 cases — empty/id/concat/partial-fail/all-fail). | E2E: helm upgrade with the `feat/swiss-tsl-trust-store` image; POST `/system/trust-store/refresh` returns 200 with `cert_count` ≈ EU + CH combined; re-index `main_signed.pdf` → `signature_status="signed_valid"`. | Trust-store metadata via GET `/system/trust-store` returns `builder_id="eu_lotl+swiss_tsl"`. Postgres `memory_items` row for `main_signed.pdf` carries the new `signed_valid`. ChromaDB chunks reflect the same. MinIO bundle SHA-256 changes from the EU-only sha to the EU+CH sha after refresh. |

## Out of scope (deferred to future ADRs)

- **Automatic TSLO cert rotation.** Today's chart-vendored TSLO is OOB-verified at vendoring time; rotation is a maintainer release task. Future ADR may add a CronJob that pulls the new TSLO cert, verifies its SHA-1 against an externally-pinned source, and updates the ConfigMap automatically — but only if a customer asks (the cost of getting it wrong is the whole chain of trust, so manual is the safer default for now).
- **Other non-EU jurisdictional TSLs** (UK, Norway, Iceland, Liechtenstein, EFTA non-CH). Each is a one-class addition behind the existing ABC; ship when a customer's content involves those signatures.
- **Adobe AATL builder.** Still ABC-supported; no impl until a customer asks.
- **VaultTrustStoreProvider.** Still ABC-supported; same posture as ADR-052 §3.

## Update protocol for the status doc

When this PR lands: update `docs/architecture/pdf-ingestion-status.md` item #12 row to reflect EU + CH composite in the default install. The "data gap" line for Swiss-signed docs is fully closed — no residual.
