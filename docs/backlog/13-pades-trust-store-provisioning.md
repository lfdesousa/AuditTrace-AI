---
title: "pdf: provision PAdES trust store (SwissSign + EU LOTL roots) so signed PDFs validate to `signed_valid`"
labels: ["pdf", "security", "audit-grade", "trust-store"]
priority: P1
---

## Context

Tier-A PDF robustness (PR #42, `feat/pdf-robustness-tier-a`) added a
`signature_status` field to every chunk's metadata: one of
`unsigned`, `signed_valid`, `signed_invalid`, `check_unavailable`.
The implementation uses `pyhanko` + `pyhanko-certvalidator` per
gap-inventory item #12 (`docs/architecture/pdf-ingestion-gaps.md §2.3`).

The deployed image's PAdES trust context is whatever
`pyhanko-certvalidator` ships with by default — which on inspection
does not include the SwissSign root chain. Live evidence captured
2026-05-07 against `main_signed.pdf` (a SwissSign-signed framework
paper) returned `signature_status="signed_invalid"` on **all 46
chunks**.

The code is correct: the signature exists, the chain does not validate
against the configured trust roots, the audit field reflects that
honestly. The **data** is wrong: a legitimately signed document is
flagged as if it were tampered with.

### Why this blocks UI

Per `project_pre_ui_critical_inventory.md §1`, this is a hard prereq
for any end-user PDF upload UI. Without the trust store provisioned:

- Every legitimately signed PDF a user uploads flags as
  `signed_invalid` in the audit log — actively misleading.
- The audit signal `signed_invalid` becomes useless: it covers both
  "tampered after signing" (the security case we want to surface) and
  "signing CA not in our trust store" (a configuration gap). The
  signal must mean only the former.

### What "trust store" means here

Three layers, in increasing scope:

1. **SwissSign roots** — minimum to validate `main_signed.pdf` itself
   and any document signed by SwissSign (the demo's signing CA,
   common for Swiss/CH-jurisdiction signatures).
2. **EU Trusted List (LOTL)** — the union of national EU member-state
   trusted lists for qualified electronic signatures (eIDAS Annex IV).
   Required if a customer-facing claim mentions eIDAS / qualified
   signatures.
3. **Adobe AATL + EUTL** — the broader commercial signature trust
   ecosystem (Adobe Approved Trust List + EU Trusted List). Closest
   thing to "what Acrobat trusts." Largest, most maintenance burden.

v1 should ship layer 1 (SwissSign) at minimum. Layer 2 (EUTL) is the
right target for any eIDAS conversation. Layer 3 is overkill until a
customer asks.

## Fix sketch

### Primary — mounted ConfigMap of PEM-encoded roots

1. **Source the chain.** Download SwissSign roots from
   `https://www.swisssign.com/en/about-us/trust-services/repository`
   (or whichever official PKI repository SwissSign currently
   publishes). Verify SHA-256 against the published fingerprints
   (out-of-band over HTTPS to a different domain). Vendor under
   `charts/audittrace/trust-store/swisssign/*.crt`.

2. **Mount as ConfigMap.** Helm chart change: new
   `charts/audittrace/templates/trust-store-configmap.yaml` reading
   `.Files.Glob "trust-store/**/*.crt"`, mounted at
   `/etc/audittrace/trust-store/` in the memory-server pod.

3. **Wire into `pyhanko`.** Build a
   `pyhanko_certvalidator.ValidationContext(trust_roots=[...])`
   from the mounted directory at startup, pass it to every
   `validate_pdf_signature(...)` call inside
   `_pdf_signature_status` (the function added in PR #42 — confirm
   exact name + module against `src/audittrace/services/pdf_*.py`
   when picking this up).

4. **Live verification.** After deploy, re-run
   `POST /memory/index?file=episodic/main_signed.pdf` and assert
   `signature_status="signed_valid"` on all chunks. Capture the
   ChromaDB query as evidence per the test-and-evidence gate
   (`feedback_test_and_evidence`).

### Secondary — pull EUTL programmatically at deploy time

Init container that fetches the EU LOTL XML
(`https://ec.europa.eu/tools/lotl/eu-lotl.xml`), validates its
signature, walks to each member-state TSL, and assembles a
trust-roots bundle on a `ReadWriteOnce` PVC mounted alongside the
ConfigMap from step 1. Refresh weekly via a CronJob.

This is genuinely larger work — defer until the v1 demo wants
"qualified eIDAS signatures validate" as an explicit claim. For now,
the static SwissSign ConfigMap is enough.

### Tertiary — distinguish trust-store-miss from chain-invalid

Even with a richer trust store, there will be PDFs signed by CAs the
operator does not trust. Today both cases collapse to `signed_invalid`.
Worth widening the taxonomy:

- `signed_valid` — chain validates, content hash matches.
- `signed_invalid` — chain validates, content hash **does not** match
  (tampering signal; the case we want surfaced).
- `signed_untrusted` — signature structurally valid, signing CA not
  in our trust store (configuration / scope signal).
- `signed_expired` — signing cert had expired at signing time and no
  LTV data is present.
- `check_unavailable` — pyhanko / certvalidator missing or errored.

Ship layer 1 (SwissSign) on the existing 4-class taxonomy, then
expand to 5 classes in a follow-up PR. Document the migration in an
ADR (likely a successor to ADR-049's evidence catalog).

## Acceptance

- `main_signed.pdf` indexed against the deployed image returns
  `signature_status="signed_valid"` on all 46 chunks.
- A deliberately tampered copy of the same PDF (one byte flipped
  post-signing) returns `signed_invalid` — captured as a fixture
  test and as live evidence.
- A self-signed PDF whose CA is **not** in the bundle returns either
  `signed_invalid` (with current 4-class taxonomy) or
  `signed_untrusted` (if tertiary fix landed) — documented either way.
- Trust-store source + provenance documented in the ADR or
  `docs/guides/trust-store.md`: which roots, where downloaded from,
  what fingerprint, who verified them.
- Helm chart values surface the trust-store path as a tunable so an
  operator with a different jurisdictional posture can swap roots
  without rebuilding the image.

## Pre-work findings (2026-05-08)

Pickup attempt 2026-05-08 surfaced these facts before pausing for
out-of-band verification:

- **Chain of `main_signed.pdf`** (verified via pyhanko inspection):
  - Signer: `LUIS ATALAIA NUNES DE SOUSA` (CH)
  - Issuer: `SwissSign RSA SIGN ZertES QES ICA 2023 - 1`
  - Sub-issuer: `SwissSign RSA SIGN Signature Services Root CA 2023 - 1`
  - **Root (self-signed):** `SwissSign Signature Services Root 2020 - 2`
- **Root SHA-256 (from embedded copy in main_signed.pdf):**
  `b87f292a4d9feace2d669159eb26f56d85ec77c19e01098cd754e8abb310cde5`
- **SHA-1:** `425419cd83663ae8815437bbeef09b15e3723e39`
- **Validity:** 2020-10-07 → 2050-09-30
- **Serial:** `2fdba9b88d001ebce7b99c2d23ef4a`

**Out-of-band sources attempted (none succeeded automated):**

| Source | Result |
|---|---|
| `repository.swisssign.com` (guessed filenames) | 404s on every shape tried |
| `swisssign.net/cgi-bin/authority/download/<sha256>` | 303 → reCAPTCHA gate (not automatable) |
| Mozilla CCADB `AllCertificateRecordsCSVFormatv2` | does not list ZertES roots (CCADB scope is WebPKI/TLS) |
| `certifi` (Python's bundle) | not present (137 certs scanned, none match) |
| Debian `ca-certificates` (`/etc/ssl/certs`) | only older Gold/Silver G2 SwissSign roots present |
| `crt.sh` JSON API | 502 (downstream issue, retry later) |

**Why this root is missing from default bundles:** Mozilla's CA
Program — the source for both `certifi` and Debian's
`ca-certificates` — is scoped to WebPKI (TLS server certificates).
ZertES / eIDAS qualified-signature roots, including SwissSign
Signature Services Root 2020 - 2, are not in scope. They are
distributed via document-signing trust lists (Microsoft AATL, EU
LOTL, Swiss federal TSL).

## Resume trigger (BEFORE picking up again)

Luis must complete one of these out-of-band verification paths and
stage the verified PEM at `/tmp/swisssign-root-2020-2-verified.pem`
before this issue can resume:

1. **SwissSign reCAPTCHA download.** Visit
   `https://swisssign.net/cgi-bin/authority/download/B87F292A4D9FEACE2D669159EB26F56D85EC77C19E01098CD754E8ABB310CDE5`,
   solve the reCAPTCHA, save the file. Compute
   `sha256sum` and confirm it equals `b87f29...0cde5`.
2. **Microsoft AATL bundle.** Pull the AATL CSV/CAB; Adobe document
   signers should include this root.
3. **Swiss federal TSL.** OFCOM/BAKOM publishes the Swiss Trust List
   in eIDAS-compatible XML. The SwissSign Signature Services Root
   2020 - 2 is the anchor for all Swiss qualified-signature
   ICAs, so it must be in the TSL.

The `/tmp/swisssign-root-2020-2.pem` file produced by the pickup
attempt (extracted from `main_signed.pdf`) has the matching SHA-256
but is **not** out-of-band verified — do not use it as-is.

## Cross-references

- `project_session_20260507.md` — original `signed_invalid` ×46
  observation during tier-A live-evidence capture (PR #42).
- `project_pre_ui_critical_inventory.md §1` — flagged as a hard
  prereq for any end-user UI for PDF upload.
- `docs/architecture/pdf-ingestion-gaps.md §2.3` — gap-inventory
  items #11 (presence), #12 (validity), #13 (LTV); this issue
  closes the back half of #12.
- `docs/ADR-048-…` (Proposed) — the security-side ingestion gate;
  trust-store is the audit-side companion. Both must land before
  v1 external upload.
- `pyproject.toml:55-56` and `requirements.txt:29-30` — pyhanko +
  pyhanko-certvalidator; the libraries this issue configures.
- `main_signed.pdf` — the canonical SwissSign-signed test fixture
  at the repo root.
