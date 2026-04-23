# ADR-045 — Laptop-first deployment: no LAN hardcodes in the chart

**Status:** Proposed
**Date:** 2026-04-23
**Supersedes / amends:** ADR-028 (observability aggregation stack),
partially realises ADR-041 Profile A ("Z-book, everything bundled")
**Related:** ADR-033/034 (k3s migration), ADR-042 (OIDC BFF for browser UIs)

## Context

The k3s migration that shipped 2026-04-17 declared `charts/audittrace/`
the sole deployable unit. The chart as-shipped hardcoded two LAN
addresses:

- `192.168.1.231` — the sibling observability stack's host (Langfuse,
  Tempo, Loki, Prometheus, Grafana).
- `192.168.1.100` — the home workstation running llama.cpp.

These IPs only resolve on the home network. A laptop install fails on
phone hotspot, on a train, or in airplane mode. The upcoming customer
Call 2 rehearsal gate (per `feedback_demo_rehearsal_non_home_network`)
cannot pass with this posture.

The initial diagnosis — "remote reach gap, add Tailscale" — was the
wrong framing. The correct framing is laptop-first sovereignty: the
laptop **is** the cell. No LAN hop should be on any critical path.

## Decision

Two durable rules, plus one implementation pattern:

### Rule 1 — Sibling stacks stay sibling

Langfuse (`~/work/langfuse/`) and the observability stack
(`~/work/observability-stack/`) remain independent docker-compose repos
that the operator starts themselves on whichever host will run
AuditTrace-AI. When the operator is mobile, they start these on the
laptop itself. The chart does **not** absorb them.

Same rule for llama.cpp: host process (recommended: `systemd --user`
units), never a Pod, never dockerized by AuditTrace-AI.

LibreChat (ADR-042) follows the same pattern: sibling compose on host,
not a chart workload.

### Rule 2 — No LAN-specific address in the chart

The chart must never ship with a hardcoded `192.168.x.y` default. Every
pod-to-host coupling resolves via `global.hostNodeIP`, which defaults to
`10.42.0.1` (the k3s flannel `cni0` bridge gateway — stable regardless
of upstream network state). Operators who want to egress to a different
host override with `--set global.hostNodeIP=...` (or
`--set externalLLM.hostIP=...` for the LLM alone).

### Pattern — the three DNS primitives

| Purpose | Mechanism | Address |
|---|---|---|
| Browser / CLI → Istio Gateway | `/etc/hosts` | `127.0.0.1 audittrace.local` |
| Pod → host (sibling stacks + llama.cpp) | k3s `cni0` bridge | `global.hostNodeIP` (default `10.42.0.1`) |
| Pod → pod (in-cluster services) | k3s CoreDNS | `*.audittrace.svc.cluster.local` |
| LibreChat container → Istio Gateway | Docker `extra_hosts` | `audittrace.local:host-gateway` |

Nothing else — no mDNS, no avahi, no `host.docker.internal`, no
environment-dependent DNS trickery.

## Consequences

### Positive

- **Three network scenarios collapse to one install.** Home WiFi, phone
  hotspot, airplane mode all use the same binary, the same Helm
  release, the same endpoints. No profile switch.
- **Demo-rehearsal discipline becomes a physical test** (`rfkill block
  all && make smoke`), not a code path.
- **`/v1/chat/completions` stays byte-identical.** Both entry points —
  OpenCode (Device Flow) and the upcoming LibreChat BFF — keep working
  unchanged. `feedback_openai_schema_inviolate` holds.
- **No new chart workloads, no new subcharts.** The review surface for
  this change is five file edits, one deletion, and two new docs.
- **Homelab regression-proof.** Users who want the 192.168.1.231 /
  192.168.1.100 posture get it with `--set`; the old manifest is
  recoverable byte-for-byte.

### Negative / accepted trade-offs

- **The operator runs four things** on the laptop before `helm install`:
  `/etc/hosts` line, mkcert CA, three systemd-user llama.cpp units, two
  docker-compose stacks. The runbook automates all of it; it's still
  four prerequisites, not zero.
- **`cni0` bridge IP is k3s-flannel-specific.** Non-flannel CNIs use
  different addresses. `detect-k3s-bridge.sh` auto-detects; operators
  on alternative CNIs will need to `--set global.hostNodeIP=<their
  bridge>` explicitly.
- **Pod → host traffic crosses the CNI bridge** (not host-network).
  Adds one hop vs. running the sibling stacks inside the cluster. For a
  laptop with ~35 GB of stack, the latency overhead is negligible;
  noted here so the next reader doesn't wonder.
- **No in-cluster obs UI on the laptop yet.** The Langfuse / Grafana /
  Tempo UIs come from the sibling composes on host ports
  (`localhost:3000`, `:3001`, `:3200`). If the operator stops those
  composes, the reconstructibility walkthrough is unreachable — even
  though trace data at the Postgres + stdout layer is still intact.
  This is not a regression (it matches current behaviour); it is a
  known constraint the customer-demo rehearsal must respect.

### Explicit non-decisions

- Bundling Langfuse / Tempo / Loki / Grafana / Prometheus inside the
  Helm chart is **not decided** by this ADR. If a future requirement
  forces a single-artifact install (e.g. a customer who cannot run
  sibling composes), a separate ADR revisits the question.
- Retirement of `~/work/observability-stack/` is **not decided**. It
  stays as the home-workstation sibling for when the chart is installed
  with `--set global.hostNodeIP=192.168.1.231`.
- Vault integration (ADR-040) is a separate stream and is not blocked
  by or blocking this ADR.

## Verification

Four gates, in order:

1. **Static grep:** `grep -rE '192\.168\.1\.(100|231)' charts/ scripts/`
   returns empty.
2. **Default-render gate:** `helm template charts/audittrace` produces
   no `192.168.1.*` strings.
3. **Homelab-override render gate:** `helm template charts/audittrace
   --set global.hostNodeIP=192.168.1.231 --set
   externalLLM.hostIP=192.168.1.100` produces the pre-change manifest
   (modulo the deleted `values-local.yaml`).
4. **Airplane smoke** on the laptop: `sudo rfkill block all && sleep 3
   && make up && curl -fk https://audittrace.local/health && curl -fk
   -H "Authorization: Bearer $(cat ~/.config/audittrace/token)"
   https://audittrace.local/v1/chat/completions -d '{...}'`. Evidence
   captured per `feedback_test_and_evidence`.

## References

- ADR-028 (observability aggregation stack) — this ADR amends the
  sibling-compose posture to apply on the laptop as well as the home
  workstation.
- ADR-041 (product boundary and dependencies) — this ADR is the
  concrete realisation of Profile A's "laptop, everything local"
  position, **without** bundling the obs stack in the chart.
- ADR-042 (OIDC Authorization Code + PKCE, BFF-first) — the LibreChat
  BFF runs as a sibling compose per the rule above, with
  `extra_hosts: ["audittrace.local:host-gateway"]`.
- `docs/guides/zbook-runbook.md` — operator-facing companion.
