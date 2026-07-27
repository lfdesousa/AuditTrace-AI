# ADR-061 — Safe deploys and independent deploy verification

- Status: Accepted
- Date: 2026-07-27
- Supersedes / relates to: operational deploy tooling under `scripts/deploy/`

## Context

Deploying the memory-server to the single-node k3s cluster was neither safe nor
independently checkable:

1. **Unsafe rollout.** The memory-server `Deployment` had no explicit `strategy`, so it
   inherited the Kubernetes default `RollingUpdate maxSurge=25%`. On `replicaCount=1`
   that rounds up to 1 and starts a **second** pod (app + sidecars) on the one node. On
   2026-07-26 that surge cascaded (CPU contention → the vault sidecar was not injected →
   the entrypoint fail-closed with exit 79 → istiod degraded → the cluster needed a restart).
2. **No independent verification.** A successful `helm upgrade` (exit 0) says nothing about
   whether the deploy actually *works*. "Up" was being inferred from the deploy command, not
   from the running system.
3. **Fragile tooling.** Ad-hoc deploy/verify steps used substring URL checks (a security-lint
   class) and did not handle transport timeouts, so a slow endpoint could crash a tool with
   no result.

## Decision

Introduce deterministic deploy and **independent** verification tooling, and make the rollout
safe by default.

1. **Surge-safe rollout.** The memory-server `Deployment` gets a values-driven `strategy`
   defaulting to `maxSurge=0, maxUnavailable=1`. This is correct for both single-node (the old
   pod terminates before the new one starts, so the node never runs two pods) and multi-replica
   HA (rolls one pod at a time, keeping N-1 Ready — zero downtime, no added node pressure).
2. **Deterministic deploy runner** (`scripts/deploy/runner.py`). Ordered, idempotent phases:
   preflight (abort on an unhealthy vault-injector/istiod) → resolve the target version to a
   **published digest** → `helm upgrade` deploying by `repository:tag@digest` (the digest pin is
   enforced at apply time), idempotent (converges on the digest, skips a no-op) → bootstrap →
   settle (assert peak concurrent Running pods ≤ replicas) → report. **The deploy runner does
   not certify health** — its report carries `certified: null`.
3. **Independent verification runner** (`scripts/deploy/verify.py`). It re-derives health itself
   from the cluster API (read-only `kubectl`/`helm`) and the public front door, emitting a
   PASS/FAIL verdict from five probes: pods-ready (exact label match), digest-matches-published,
   no-drift (release deployed + surge-safe strategy intact), health-version, and a real
   front-door recall end-to-end. It **never reads the deploy runner's report** — the
   `reads_deploy_report` field is derived from the enumerated evidence the verdict stands on. A
   FAIL blocks "done".
4. **Robust, safe tooling.** All HTTP egress goes through a single seam per module that handles
   the full transport-error class (`OSError`, which subsumes `TimeoutError`/`URLError`), so a
   slow or unreachable endpoint yields a clean failure (a probe FAIL / an empty best-effort
   result / a typed error) rather than an uncaught crash. URL host checks use `urlparse().hostname`
   exact comparison, never substring matching. Outcomes are recorded to the internal memory
   server (`scripts/deploy/memory.py`).

The separation is the point: whether a deploy *happened* and whether it *works* are decided
separately, on independently gathered evidence.

## Consequences

- A routine version bump can no longer surge a second pod onto the single node — the
  2026-07-26 cascade root cause is closed at the chart level.
- "Deployed" and "healthy" are distinct, evidence-backed states; a green deploy cannot assert
  health — only the independent verifier can, and it can FAIL.
- The verifier is falsifiable in the field: injecting a strategy drift (`maxSurge=1`) yields
  `VERDICT: FAIL` on the no-drift probe (the other probes still PASS), and restoring the
  surge-safe strategy returns `VERDICT: PASS`.
- All deploy tooling is unit-tested (per-file ≥90% line+branch) and validated live on the
  laptop k3s against the published image.

## Evidence

- Surge-safe roll: a real rollout where peak concurrent memory-server pods = 1 (no surge).
- Deploy runner: real-mode run resolved the published digest and converged idempotently.
- Verify runner: `VERDICT: PASS` on the healthy deploy (all five probes; `reads_deploy_report=false`);
  fault-injection cycle PASS → drift → **FAIL** (no-drift) → restore → **PASS**.
