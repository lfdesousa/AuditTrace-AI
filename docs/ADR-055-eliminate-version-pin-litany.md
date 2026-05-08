# ADR-055 — Eliminate the version-pin litany

**Status:** Accepted
**Date:** 2026-05-09
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-049 (Test, Evidence, Reconstructibility Gate), `feedback_no_more_drifts`, `tests/test_version_drift.py` (the existing drift-guard the v1.0.10→v1.0.11 incident left behind).

## Context

Twelve releases — v1.0.1 through v1.0.12 — silently misreported the running release at runtime. The release-time litany was supposed to bump four sites in lockstep (`pyproject.toml::version`, `models.py::HealthResponse.version`, `server.py::_resolve_version` fallback, `values.yaml::OTEL_RESOURCE_ATTRIBUTES service.version`). At least three drift incidents have been documented:

1. **v1.0.10→v1.0.11** (2026-05-06) — tag bumped, three Python sites missed. Caught next session; pinned by `tests/test_version_drift.py`'s first three test cases.
2. **OTEL `service.version=1.0.0`** — frozen since v1.0.0; twelve releases shipped to Tempo + Langfuse self-identifying as v1.0.0. Caught 2026-05-09 by Luis; pinned by `test_pyproject_matches_chart_otel_service_version`.
3. **v1.0.13 image misreported as v1.0.11** (caught 2026-05-09 during runbook validation) — the Dockerfile never actually installed the `audittrace-ai` package; it just `COPY src/ ./src/`'d the source tree. A stale `src/audittrace_ai.egg-info/PKG-INFO` from a developer-side `pip install -e .` got baked into the image. `importlib.metadata.version("audittrace-ai")` happily returned the stale `1.0.11` from that egg-info. Plus there was no `.dockerignore` to keep build artefacts out of the build context.

A fourth pin site also turned up while writing this ADR: **`Chart.yaml::appVersion`** has been frozen at `"1.0.0"` since v1.0.0. Every k8s object's `app.kubernetes.io/version` label across twelve releases said `1.0.0`. Same drift class, missed by the existing test suite because it didn't check the chart.

Each fix has been a successful one-off: catch the drift, bump the pin, add another test to the drift-guard. The pattern works but the underlying problem is **duplication of the same fact across multiple files**. Every release re-introduces the risk that one site falls out of sync. The drift-guard catches it after the fact; the consolidation prevents it.

This ADR records the move from "N pin sites + N drift tests" to "one source of truth + one drift assertion + one release-script target."

## Decision

Six concrete decisions, all delivered in the v1.0.14 release.

### #1 — Drop hardcoded version literals from `models.py` + `server.py`

**Decision.** Remove the `"1.0.13"` constants from both files. `HealthResponse.version` default becomes `"unknown"` (route handler always overrides). `server._resolve_version()` falls through to `"unknown"` if `importlib.metadata.version("audittrace-ai")` raises. Both make the dev-tree state visible, instead of pretending to be a release that the developer hasn't actually built.

**Why "unknown" rather than reading pyproject at runtime:**

1. Reading `pyproject.toml` at runtime is brittle — the file's location depends on how the package is installed. In a wheel install, pyproject isn't shipped (only the `.dist-info/METADATA`). In an editable install, it is — but at a path discovered via package metadata, which is the same lookup `importlib.metadata.version()` already does.
2. `importlib.metadata` IS the right path. The fix is making sure the package metadata exists in the deployed image (decision #4) — not adding a parallel source-of-truth lookup.
3. `"unknown"` in `/health` is a clear signal that the operator is running an uninstalled source tree, not a packaged release. That's information, not noise.

### #2 — `Chart.yaml::appVersion` becomes the chart-side single source for OTEL + k8s labels

**Decision.** `OTEL_RESOURCE_ATTRIBUTES` is removed from `values.yaml::memoryServer.env` and instead built in `templates/memory-server/deployment.yaml` from `{{ .Chart.AppVersion }}`. The `app.kubernetes.io/version` label (already templated from `.Chart.AppVersion` in `_helpers.tpl`) automatically picks up the same source.

```yaml
# Before — values.yaml had a hardcoded version token in a giant string:
OTEL_RESOURCE_ATTRIBUTES: "service.namespace=audittrace,service.version=1.0.13,deployment.environment=local"

# After — deployment.yaml computes it from chart metadata:
- name: OTEL_RESOURCE_ATTRIBUTES
  value: "service.namespace=audittrace,service.version={{ .Chart.AppVersion }},deployment.environment={{ .Values.global.environment | default "local" }}"
```

**Why move it from values.yaml to the template:**

The values file is operator-overridable per deployment; the version is not — every install of a v1.0.14 chart MUST report v1.0.14, regardless of operator overlays. Putting the version in a template that pulls from `Chart.AppVersion` (which is fixed at chart-package time) is the architecturally honest place. Operators can still override `OTEL_RESOURCE_ATTRIBUTES` wholesale in their values overlay if they want a different namespace or environment label, but the version stays accurate.

### #3 — `Chart.yaml::appVersion` MUST equal `pyproject.toml::version` (drift assertion)

**Decision.** `tests/test_version_drift.py` collapses from three pin assertions (one per Python site + one for OTEL) to **one** assertion: `Chart.AppVersion == pyproject.version`. The other previously-pinned sites no longer have version literals to drift against, so the test cases for them become redundant and are removed.

**Why drop the existing per-file tests rather than keep them:**

`models.py` no longer has a version literal (default is `"unknown"`). `server.py` no longer has the fallback constant (returns `"unknown"`). `values.yaml` no longer has the OTEL_RESOURCE_ATTRIBUTES fragment with a version. The tests would be asserting against absent strings — meaningless. Replacing four assertions with one that's actually load-bearing is the cleanup, not a regression.

### #4 — Dockerfile installs the package itself so `importlib.metadata` works

**Decision.** Stage 1 of the Dockerfile gains `RUN pip install --user --no-deps -e /build/audittrace-ai-src` (or equivalent — see implementation). The package metadata gets generated from the current `pyproject.toml` at build time and lands in `/root/.local/lib/.../site-packages/audittrace_ai-<version>.dist-info/METADATA`. Stage 2 inherits via `COPY --from=builder /root/.local /home/sovereign/.local`. At runtime `importlib.metadata.version("audittrace-ai")` returns the real installed version.

**Why `--no-deps`:**

The dependency install (`pip install -r requirements.txt`) already happened in the prior Dockerfile step. Re-resolving deps just for the package metadata install is wasted layer work. `--no-deps` skips that step.

**Why `-e` (editable) rather than a wheel build:**

The runtime image already does `COPY src/ ./src/` for the source. An editable install in the builder stage just generates the `.dist-info` next to the source-pointer; no double-copy. Wheel-building is the right answer for a published artefact, but for an in-house image-build path it's extra ceremony.

### #5 — `.dockerignore` prevents stale build artefacts from re-introducing drift

**Decision.** New `.dockerignore` excludes Python build artefacts (`*.egg-info/`, `*.egg`, `*.dist-info`, `__pycache__`, `.pytest_cache`, etc.), virtual environments (`.venv/`, `venv/`, `env/`), IDE noise, git metadata, and local secret files from the Docker build context.

**Conservative inclusion list:** `tests/`, `charts/`, `docs/` are KEPT in the build context because the multi-stage Dockerfile's Stage 3 (`tests` image) `COPY tests/ /app/tests/` and operators may build chart-aware images for other purposes. Excluding these would break Stage 3.

This closes the v1.0.13 drift mode (stale developer-side `egg-info` baked in via the implicit `COPY src/`) at the build-context level — a defence in depth alongside the proper `pip install -e .` in the builder stage.

### #6 — `make release VERSION=...` target scripts the release bumps

**Decision.** New Makefile target:

```bash
make release VERSION=1.0.14
# Bumps:
#   - pyproject.toml::version
#   - charts/audittrace/Chart.yaml::appVersion
# Regenerates:
#   - tests/fixtures/openapi.snapshot.yaml
#   - docs/reference/audittrace/openapi.yaml
# Verifies:
#   - tests/test_version_drift.py passes
#   - make test passes
# Stages and shows the diff for the operator to commit + tag.
```

**Why a Makefile target rather than a script in scripts/:**

Existing release-side automation (`make sync-requirements`, `make test`, `make lint`) is in the Makefile. Operators expect to find release-time rituals there. Symmetry beats novelty.

The target deliberately stops short of committing or tagging — it's a "prep my release" command, not a "release on my behalf" command. Tag-pushing remains an explicit human step (per `feedback_evidence_self_check_before_commit`) so the gate-self-check is preserved.

## Consequences

### Positive

- **Three pin sites collapse to two** (pyproject.toml::version + Chart.yaml::appVersion). Every other place that used to need bumping is now templated/dynamic.
- **The drift class closes for good.** The remaining single drift assertion (chart appVersion == pyproject version) is mechanically gated; future releases that miss either fail CI before tag-push.
- **`make release VERSION=...`** removes the recall-from-memory step that has caused at least three drift incidents.
- **Honest dev-tree behaviour.** Running from a non-installed source tree shows `version: "unknown"` in `/health` rather than lying about being some old release.
- **Image-build hygiene.** `.dockerignore` prevents the build context from carrying whatever stale artefacts the maintainer's laptop happens to have.

### Negative

- **`Chart.AppVersion` becomes load-bearing for runtime observability** (Tempo / Langfuse / k8s labels). Bumping pyproject without bumping Chart.yaml::appVersion produces a hard CI failure on the drift assertion — by design, but operators not used to thinking about Chart.yaml may stumble. Mitigated by the `make release` target.
- **`/health` reports `"unknown"` in dev trees.** Documented in the deployment runbook as the expected behaviour for source-tree runs; not a bug.

### Risks

- **Existing v1.0.13 image is broken in production observability** — Tempo + Langfuse traces from that release self-identify as v1.0.11. v1.0.14 is the first release that reports correctly. Treat v1.0.13 as a "broken-image release" in trace queries; v1.0.13 ChromaDB / Postgres data is unaffected (the trust-store work itself is correct).
- **`make release` script edge cases.** First runs may surface issues with how it reads pyproject + writes Chart.yaml. Mitigated by the drift assertion catching any mismatch before tag-push.

## Validation per the gate (ADR-049 §Decision)

| Verification | Validation | Reconstruction |
|---|---|---|
| `make test` green; `make lint` + `make format` clean; `tests/test_version_drift.py::test_chart_appversion_matches_pyproject_version` PASS; existing pin tests removed (the literals they checked no longer exist). | E2E: `make release VERSION=1.0.14`; build + push image; helm upgrade; `GET /health` returns `version="1.0.14"` (verified inside the pod via `importlib.metadata.version("audittrace-ai") → "1.0.14"`). Re-run the trust-store refresh; manifest still flips correctly. | Pod logs show `app.kubernetes.io/version: 1.0.14` label on resources; Tempo trace attributes show `service.version=1.0.14`; OpenAPI spec self-identifies as v1.0.14. |

## Out of scope (deferred)

- **Auto-publishing chart releases via OCI registry.** When the chart is published as an OCI artefact, `make release` could push to the registry too. Not needed today (single-tenant deployment).
- **CI auto-bump on tag push.** Could mechanise the bumps from a workflow rather than a developer-run Makefile target. Premature for the current single-maintainer release cadence.
- **GitHub release notes auto-generation.** Different concern; currently handled in the GitHub UI on tag publish.

## Update protocol for the runbook

The deployment runbook's release section (added in v1.0.13) gets a small amendment: the litany of "bump four pin sites in lockstep" becomes "run `make release VERSION=X.Y.Z`, review the diff, commit, tag." One step instead of five.
