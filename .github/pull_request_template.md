<!--
This template is enforced by ADR-049 (Test, Evidence, and
Reconstructibility Gate) via the CI `evidence-check` job. Each of
the three required sections below MUST contain at least one
populated bullet (not just the empty checklist line). Missing or
empty sections fail the merge gate. There is no override.

Mapping to V&V + Sovereignty-Reconstructibility Gap:
  - Verification = "are we building the artefact right?"
  - Validation   = "are we building the right thing for the user?"
  - Reconstruction = "can a third party prove from durable artefacts that this worked?"

For docs-only / refactor PRs that genuinely don't change runtime
behaviour, populate Validation with "no runtime behaviour change
— diff is documentation/refactor only" + the rationale, and
Reconstruction with the corresponding `git diff --stat` summary.
-->

## Summary

<!-- 1-3 sentences. What changed and why. -->

## Verification (unit)

- [ ] `make test` green — paste the tail of the output:
  ```
  <paste: "X passed, Y skipped, Z% coverage" + per-file gate result>
  ```
- [ ] Per-file coverage ≥ 90% — paste `check-per-file-coverage.py` output:
  ```
  <paste: "per-file coverage gate: PASS (N files checked, all >= 90%)">
  ```

## Validation (end-to-end against deployed image)

- [ ] Image tag deployed:
  `localhost:5000/audittrace/memory-server:<tag>`
- [ ] `verify-deploy` summary: `<X passed | Y failed | Z skipped>`
- [ ] API call exercised through the public surface:
  - Endpoint: `<METHOD /path>`
  - Scope used: `<scope set on the JWT>`
  - Authentication: Device Flow / Client Credentials / Bypass mode
- [ ] Response body or relevant excerpt:
  ```
  <paste: response JSON or relevant fields>
  ```

## Reconstruction (audit artefacts)

<!-- These let a reviewer reproduce the validation verdict without
     having been present when the work was done. -->

- [ ] Trace ID (Tempo / Langfuse): `<id>`
- [ ] Audit row in `interactions`: `<row id>` or
  `SELECT * FROM interactions WHERE trace_id = '<id>'` (paste rowcount)
- [ ] Side-effect query (ChromaDB / MinIO / Postgres) confirming
  the change:
  ```
  <paste: query + result, e.g. ChromaDB metadata for a freshly-indexed chunk>
  ```

## Notes for reviewer

<!-- Anything the reviewer needs to know that isn't obvious from
     the diff or the artefacts above. Risks, rollback plan,
     follow-ups. Optional. -->
