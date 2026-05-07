---
title: "build: `make sync-requirements` + pre-commit hook for pyproject↔requirements.txt drift"
labels: ["build", "tech-debt", "pre-commit", "ci"]
priority: P2
---

## Context

`pyproject.toml` and `requirements.txt` declare the same Python deps in
two places. The image build path uses `requirements.txt`; the test /
local-dev path resolves from `pyproject.toml`. There is currently no
automation that keeps them in lockstep, and no check that fails when
they diverge.

### Observed incident

**2026-05-07 evening (tier-A live-evidence capture, PR #42).** The
tier-A morning commit added `pyhanko>=0.21.0` and
`pyhanko-certvalidator>=0.26.0` to `pyproject.toml` (lines 55–56) but
**not** to `requirements.txt`. `make test` passed locally because the
test environment installed from `pyproject.toml`. The Docker image
built from `requirements.txt` and silently shipped without the
signature-validation deps. Live evidence then showed
`signature_status="check_unavailable"` on all 46 chunks of
`main_signed.pdf` — the code's graceful-degradation branch, not a
signature-validity result.

The fix landed as commit `3cfc09d` on the same PR
(`fix(pdf): add pyhanko + pyhanko-certvalidator to requirements.txt`)
and the post-fix evidence flipped to `signature_status="signed_invalid"`
×46 (correct: trust store has no SwissSign roots — see backlog #13).

The structural risk remains: the next dependency added to
`pyproject.toml` will reproduce this exact silent-image-degradation
unless the two files are kept in sync mechanically.

## Fix sketch

### Primary — `make sync-requirements` target + pre-commit hook

1. **Generate `requirements.txt` from `pyproject.toml`.** Use
   `pip-compile` (from `pip-tools`) or `uv pip compile`:

   ```makefile
   sync-requirements:
   	uv pip compile pyproject.toml -o requirements.txt --no-header --quiet
   ```

   Decide upfront whether `requirements.txt` should be:
   - **Loose constraints** — copy `pyproject.toml`'s `>=X.Y` ranges
     verbatim. Easier, but doesn't pin transitives.
   - **Fully pinned lockfile** — `pip-compile` resolves the full
     transitive closure with hashes. Reproducible builds, larger diff
     per dependency change.

   Recommend: fully pinned. The whole reason this bit was a silent
   image-build divergence; pinning closes that and gives reproducibility
   for free.

2. **Pre-commit hook that fails on divergence.** New entry in
   `.pre-commit-config.yaml`:

   ```yaml
   - id: requirements-sync
     name: requirements.txt is in sync with pyproject.toml
     entry: scripts/check-requirements-sync.sh
     language: script
     pass_filenames: false
     files: '^(pyproject\.toml|requirements\.txt)$'
   ```

   The script regenerates `requirements.txt` to a temp file and diffs
   against the working copy — non-zero exit on diff, with a one-line
   "run `make sync-requirements`" hint.

3. **CI job mirrors the hook.** Same diff check in
   `.github/workflows/ci.yml`, separate from `make test` so the
   failure mode is visible in the PR checks list.

### Secondary — drop `requirements.txt` and build the image from `pyproject.toml` directly

`Dockerfile` could `pip install .` (or `pip install -e .`) from
`pyproject.toml` directly, eliminating the second file. Smaller blast
radius for divergence (zero), but loses the lockfile property unless
combined with `uv pip compile` for reproducibility.

Worth scoping; preferred fix may be: keep `requirements.txt` as the
generated lockfile, build the image from it (as today), and add the
sync check.

### Tertiary — Dependabot wiring

Once the sync check exists, configure Dependabot to update both files
in the same PR (the `pip` ecosystem update group). Memory
`project_python_version_pin_sites` documents the broader version-pin
problem this issue is a sub-case of.

## Acceptance

- `make sync-requirements` produces a deterministic `requirements.txt`
  from `pyproject.toml`.
- Adding a dep to `pyproject.toml` without running the target causes
  the pre-commit hook to fail with a clear remediation message.
- Same check runs in CI (`requirements-sync` job in
  `.github/workflows/ci.yml`).
- The image build path is documented (in `AGENTS.md` or
  `Dockerfile` comment) as consuming the generated lockfile, not
  manually edited.
- A CI dry-run regression: temporarily add a dep to `pyproject.toml`
  only, push, confirm the PR check fails. Then run
  `make sync-requirements`, push, confirm green.

## Cross-references

- `project_session_20260507.md` — original detection during tier-A
  PR #42 live-evidence capture; bug #2 in that day's "bugs caught by
  the gate" list.
- `project_pre_ui_critical_inventory.md` §4 — flagged this as
  dev-env friction worth closing in the same calendar week as it was
  discovered.
- `project_python_version_pin_sites.md` — broader same-class issue
  (Python version pinned in 8 sites; Dependabot only touches one).
- `feedback_test_and_evidence.md` — the gate that caught this:
  unit tests passed, live evidence flipped from
  `check_unavailable` → `signed_invalid`, exposing the missing dep.
- `feedback_no_more_drifts.md` — every chart/code change that
  introduces coupling needs an automated provisioner OR a drift-guard
  test. This is the same pattern, applied to the build pipeline.
