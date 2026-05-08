# ADR-054 — PAdES as-of-signing-time validation + `signed_expired` audit class

**Status:** Accepted
**Date:** 2026-05-09
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-052 (PAdES trust store + 8-class taxonomy), ADR-053 (Swiss federal TSL + composite builder), `feedback_design_for_real_user`, `feedback_no_shortcuts`.

## Context

ADR-052 + ADR-053 closed the trust-store coverage gap (`signed_invalid` ×46 → mostly `signed_valid` for documents signed by EU-eIDAS or Swiss-federal qualified TSPs). One residual case stayed `signed_untrusted` after live evidence: `main_signed.pdf`, signed by SwissSign with a trust chain whose issuing CA (`SwissSign RSA SIGN ZertES QES ICA 2023 - 1`) IS in our composite trust bundle (verified by manual chain walk inside the pod).

Why it stayed `signed_untrusted`: the end-entity signing cert's validity window expired on **2026-04-03** — 36 days before today's evidence run. pyhanko's basic-PAdES validation (PAdES B-B profile per ETSI TS 119 142-1) walks the chain at the **current** time and rejects expired certs with `pyhanko_certvalidator.errors.ExpiredError`. The error returns to `_pdf_signature_status` as `intact=True, valid=True, trusted=False` — which our 8-class taxonomy then classified as `signed_untrusted`.

That classification is **honest but uninformative**. `signed_untrusted` reads to an auditor as "we don't know this CA" — but in this case, we know the CA fine; the signature was valid the moment it was signed; the certificate has simply expired since then. Two distinct audit categories collapsed into one class.

PAdES higher-assurance profiles handle this:
- **PAdES B-T** — embeds a Trusted Timestamp (TSA) over the signature, anchoring the signing time to a third-party clock.
- **PAdES B-LT** — adds a DSS (Document Security Store) with all CRL/OCSP responses needed to prove the chain was valid at signing time.
- **PAdES B-LTA** — extends B-LT with periodic re-timestamping for archival.

`main_signed.pdf` is presumably PAdES B-B (no embedded LTV / no TSA). Validating it post-expiry is impossible with revocation freshness checks, but the basic question — "was the signature math + trust chain valid at signing time?" — IS answerable: pyhanko exposes `embedded_sig.self_reported_timestamp` (the signer-asserted signing-time), and `ValidationContext` accepts a `moment=` parameter.

This ADR records the decision to **retry validation as-of the self-reported signing time** when basic validation reports `trusted=False`, and surface a new `signed_expired` audit class when the retry succeeds.

## Decision

Five concrete decisions, all delivered in v1.0.15 (or whatever version this PR ends up labelled).

### #1 — Add `signed_expired` as a 9th audit class

**Decision.** Extend `_SIGNATURE_STATUS_CODES` with `signed_expired`. The 9-class taxonomy:

| Class | Pyhanko basic outcome | New as-of-signing retry outcome | Audit semantic |
|---|---|---|---|
| `check_skipped` | (operator disabled) | n/a | Operator opted out |
| `check_unavailable` | (pyhanko not importable) | n/a | Runtime degraded |
| `check_failed` | (pyhanko raised) | n/a | Validation crashed; investigate |
| `none` | 0 signatures present | n/a | Document is unsigned |
| `signed_valid` | intact, valid, trusted | n/a | Chain validates against current time + roots |
| `signed_invalid` | intact, valid=False | n/a | Signature math broken (real audit signal) |
| `signed_untrusted` | intact, valid, trusted=False | retry FAILED | Chain doesn't terminate at our trust roots, even at signing time |
| `signed_expired` | intact, valid, trusted=False | **retry SUCCEEDED** | Chain validated at signing time; cert has since expired |
| `signed_tampered` | intact=False | n/a | Content modified after signing |

**Why a 9th class instead of folding into `signed_valid`:**

1. `signed_valid` means "chain validates as-of NOW." Folding `signed_expired` in would break that contract and silently widen `signed_valid` to mean "valid by some validation profile" — auditors who relied on `signed_valid` ⇒ "currently valid signing identity" would lose information.
2. `signed_expired` is genuinely informative. An auditor seeing `signed_expired` knows: signed by a trusted CA, math was valid, cert lifecycle has progressed past validity. Different remediation than `signed_invalid` (investigate tampering) or `signed_untrusted` (configure additional trust roots).
3. Closed-set discipline is preserved. `_SIGNATURE_STATUS_CODES` becomes a 9-element frozenset. `tests/test_memory_routes.py::TestSignatureStatusCodes` extends to assert the 9-class set; future additions without an ADR amendment fail CI.

**Why NOT a 10th class (`signed_revoked` for revocation):**

Revocation-after-signing is a related but distinct case that requires LTV data (CRL/OCSP responses) embedded in the document to validate retroactively. v1 doesn't ship LTV consumption; deferring `signed_revoked` to a future ADR keeps this PR scoped to the specific case live evidence revealed.

### #2 — Retry validation as-of `self_reported_timestamp` when `trusted=False`

**Decision.** When `validate_pdf_signature` returns `intact=True, valid=True, trusted=False`, the code path:

1. Reads `embedded_sig.self_reported_timestamp` (returns `Optional[datetime]` — None if no signing-time attribute is embedded in the PDF / signer_info).
2. If non-None, builds a fresh `ValidationContext(trust_roots=...same_roots..., moment=signing_time, best_signature_time=signing_time)`.
3. Re-runs `validate_pdf_signature(emb, signer_validation_context=that_vc)`.
4. If retry returns `trusted=True`, classify as `signed_expired`.
5. If retry returns `trusted=False` (or self_reported_timestamp was None), classify as `signed_untrusted` (the existing behaviour — unknown CA at any time).

**Why `self_reported_timestamp` and not a TSA-anchored time:**

`self_reported_timestamp` is what the signer asserts. A TSA timestamp (cryptographically anchored to a third party's clock) is more trustworthy but only present in PAdES B-T+ profiles. For the basic-PAdES `main_signed.pdf` case, signer-asserted is the only signal we have. Future iterations may prefer TSA-anchored when present (extract via `embedded_sig.signed_attrs` or `embedded_sig.timestamps`); for v1 the simpler path covers the immediate case.

The risk of trusting signer-asserted time: a malicious signer could backdate. This is a known limitation of basic-PAdES validation; auditors who care about temporal anchoring need PAdES B-T+ documents (which `signed_expired` flags as "signature was valid at the time the signer claimed; we believe them as much as any basic-PAdES validation").

### #3 — Re-use the same trust roots, no second Provider hit

**Decision.** The retry's ValidationContext uses the **same `trust_roots` list** the first-attempt context did. No second `Provider.load()` call. The MinIO bundle is read once at the top of `_get_validation_context`; both ValidationContext instances share the parsed cert list.

**Why:**

1. Performance — a single PEM parse and decode per request, not two.
2. Semantic consistency — both validation attempts are against the SAME trust roots. The only difference is `moment`. Using a different bundle for the retry would make the audit signal harder to reason about.
3. The `_get_validation_context` cache singleton already exposes its trust roots via the cached ValidationContext's `trust_roots` attribute (verified via pyhanko_certvalidator's API surface).

### #4 — Update precedence: `tampered > invalid > untrusted > expired > valid`

**Decision.** Multi-signature documents pick the worst class across signatures. The new ordering:

```
signed_tampered  (definitive bad)
signed_invalid   (math broken)
signed_untrusted (chain unverifiable AT ALL — strongest scope-gap signal)
signed_expired   (chain verified at signing time — weakest negative signal)
signed_valid     (currently valid)
```

**Why `signed_untrusted` outranks `signed_expired`:**

`signed_untrusted` means we have no confidence in this signing identity at all (CA not in our trust roots at any time). `signed_expired` means we have confidence in the identity (CA was trusted, signature was valid then) but the chain has aged out. A document mixing both signature types should flag the higher-uncertainty class. An auditor reviewing a `signed_untrusted` file knows the issuing CA needs investigation; reviewing a `signed_expired` file knows the document predates a cert rotation.

### #5 — `EmbeddedPdfSignature` access pattern

**Decision.** `_pdf_signature_status` already takes `embedded_sig` per loop iteration. Access pattern for the retry:

```python
status = validate_pdf_signature(embedded_sig, signer_validation_context=vc)
if not status.intact:
    any_tampered = True
    continue
if not status.valid:
    any_invalid = True
    continue
if status.trusted:
    continue  # signed_valid for this sig

# trusted=False — try as-of-signing-time
signing_time = embedded_sig.self_reported_timestamp
if signing_time is None:
    any_untrusted = True
    continue

retry_vc = ValidationContext(
    trust_roots=trust_roots_list,  # reused from outer call
    moment=signing_time,
    best_signature_time=signing_time,
)
retry_status = validate_pdf_signature(
    embedded_sig, signer_validation_context=retry_vc
)
if retry_status.trusted:
    any_expired = True
else:
    any_untrusted = True
```

**Aggregation after the loop** (with new precedence):

```python
if any_tampered: return ("signed_tampered", count)
if any_invalid: return ("signed_invalid", count)
if any_untrusted: return ("signed_untrusted", count)
if any_expired: return ("signed_expired", count)
return ("signed_valid", count)
```

## Consequences

### Positive

- **`main_signed.pdf` flips from `signed_untrusted` to `signed_expired`** — the audit signal now correctly describes "valid at signing time, cert expired since." Auditors get useful information instead of a misleading scope-gap classification.
- **8-class to 9-class taxonomy** is a strict information gain. No previously-classified document changes class incorrectly: documents that WERE truly `signed_untrusted` (unknown CA) stay that way (the retry fails for them), documents that WERE `signed_valid` are unaffected (no retry runs).
- **Honest dev-mode behaviour.** `self_reported_timestamp` returning None falls through to `signed_untrusted`. Documents without a self-asserted signing time get treated as before; no false positives.
- **Closed-set discipline preserved.** The taxonomy-pinning test extends to 9 elements. Future additions still need an ADR.

### Negative

- **Slight performance cost on `signed_untrusted` paths.** Every `trusted=False` outcome now does a second `validate_pdf_signature` call before classifying. For documents whose chains genuinely don't validate at any time, that's wasted work. Mitigation: the second validation reuses the parsed trust roots, doesn't re-fetch from MinIO, and pyhanko's chain walk is O(chain depth × trust roots) — single-digit milliseconds on a 900-cert bundle.
- **`signed_expired` introduces a new metadata cardinality.** Existing dashboards / Grafana panels that group by `signature_status` will surface a new bucket; no existing buckets change shape.
- **Trusts signer-asserted signing time.** A bad-faith signer can backdate. Documented as a basic-PAdES limitation; PAdES B-T+ profile support is a future ADR (would prefer TSA-anchored time when present).

### Risks

- **Reusing `trust_roots` from the outer ValidationContext** depends on pyhanko_certvalidator's API exposing the parsed cert list. Verified at write time (the ValidationContext object holds them). Guard with a fallback path that rebuilds from `_pem_bundle_to_cert_list` if API access changes.
- **`self_reported_timestamp` precedence over signed_attrs explicit lookup** — pyhanko's helper reads from signer_info attributes OR the PDF's `/M` field. Either source is fine for our purposes; both are signer-asserted.
- **Multi-signature documents with mixed expiry** flag as the worst class. A document with one fresh `signed_valid` sig + one `signed_expired` sig flags as `signed_expired` (the worse). Audit query for "any signature still valid" needs to read per-chunk metadata, not the aggregate. Acceptable — the aggregate is a summary.

## Validation per the gate (ADR-049 §Decision)

| Verification | Validation | Reconstruction |
|---|---|---|
| `make test` green; per-file ≥ 90% on `routes/memory.py`; new `TestPdfSignatureValidation::test_signed_expired_returns_signed_expired` covers the new path with mocked retry succeeding; existing `test_signed_with_untrusted_chain_returns_signed_untrusted` updated to mock retry FAILING (so it stays `signed_untrusted`); `TestSignatureStatusCodes` set extended to 9 elements; precedence test added covering tampered > invalid > untrusted > expired > valid. | E2E re-index of `main_signed.pdf` against the v1.0.15 image: `signature_status="signed_expired"` (was `signed_untrusted` against v1.0.14). `Luis_Research_Proposal_signed.pdf` stays `signed_valid` (regression check). | Postgres `memory_items` row + ChromaDB chunks for `main_signed.pdf` carry `signed_expired`; Tempo trace of the index call shows the retry-with-moment branch executed; the dashboard query `signature_status = "signed_expired"` returns the row. |

## Out of scope (deferred)

- **PAdES B-T / B-LT / B-LTA validation profiles** — using TSA timestamps when present, consuming embedded LTV (DSS) data for revocation-aware as-of-signing-time validation. Future ADR when a customer's documents start carrying these profiles.
- **`signed_revoked` 10th class** — distinct from `signed_expired`; needs LTV consumption to validate retroactively. Future ADR.
- **TSA timestamp validation context split** — pyhanko's `validate_pdf_signature` accepts a separate `ts_validation_context` for TSA chain validation. v1 of this ADR uses the same trust roots for both signer + TSA. Refining is a small follow-up if a customer's TSA chain doesn't overlap with our signer trust roots.
- **Auto-detection of best-available signing time** (e.g. prefer TSA when present, fall back to signer-asserted). v1 uses signer-asserted unconditionally; future iteration can plumb the preference through.
