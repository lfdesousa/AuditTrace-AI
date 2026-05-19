# ADR-045 — Laptop-first deployment: no LAN hardcodes in the chart

**Status:** Accepted (Rule 2 mechanism revised — see *Amendment 2026-05-19* below)
**Date:** 2026-04-23 (initial); 2026-05-19 (amendment)
**Supersedes / amends:** ADR-028 (observability aggregation stack),
partially realises ADR-041 Profile A ("Z-book, everything bundled")
**Related:** ADR-033/034 (k3s migration), ADR-042 (OIDC BFF for browser UIs), [[adr-007-llm-runtime]] (AWS LLM EC2 + NLB-TLS), [[adr-008-observability]] (AWS observability EC2 + NLB-TLS)

## Context

The k3s migration that shipped 2026-04-17 declared `charts/audittrace/`
the sole deployable unit. The chart as-shipped hardcoded two LAN
addresses:

- `192.168.1.231` — the sibling observability stack's host (Langfuse,
  Tempo, Loki, Prometheus, Grafana).
- `192.168.1.100` — the home workstation running llama.cpp.

These IPs only resolve on the home network. A laptop install fails on
phone hotspot, on a train, or in airplane mode. The off-LAN customer-
demo rehearsal gate (per `feedback_demo_rehearsal_non_home_network`)
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

### Rule 2 — No literal IP address in the chart (FQDN-only)

> **Revised 2026-05-19** — see *Amendment 2026-05-19* below for the
> shift from "IP fallback via `global.hostNodeIP`" to "FQDN-only via
> `Service kind=ExternalName`". The intent ("no LAN hardcode") is
> unchanged; the mechanism is improved.

The chart must never ship with a literal IPv4 address in any values
field or rendered manifest — neither a `192.168.x.y` LAN address nor a
`10.42.0.1` CNI-bridge address. Every pod-to-host coupling resolves
via a fully-qualified domain name supplied by the operator:

- `externalLLM.host` — FQDN of the host running the three
  `llama-server` ports (chat 11435 / embed 11436 / summarizer 11437).
- `observability.external.langfuseHost` / `tempoHost` / `lokiHost` /
  `prometheusHost` — FQDNs of the observability backend hosts.

The chart consumes these via `Service kind=ExternalName`, which
returns a CNAME at cluster DNS lookup time. Resolution of the CNAME
is the operator's responsibility: Route53 in the cloud, k3s CoreDNS
rewrite (or `/etc/hosts` on the host plus host-network resolver) on
the laptop.

### Pattern — the four DNS primitives (post 2026-05-19 amendment)

| Purpose | Mechanism | Address |
|---|---|---|
| Browser / CLI → Istio Gateway | `/etc/hosts` | `127.0.0.1 audittrace.local` |
| Pod → out-of-cluster host (llama.cpp + obs backends) | k3s CoreDNS rewrite → operator-managed FQDN → resolves to a host on the LAN OR to an internal NLB on AWS | `externalLLM.host`, `observability.external.*Host` — FQDNs only |
| Pod → pod (in-cluster services) | k3s CoreDNS | `*.audittrace.svc.cluster.local` |
| LibreChat container → Istio Gateway | Docker `extra_hosts` | `audittrace.local:host-gateway` |

Nothing else — no mDNS, no avahi, no `host.docker.internal`, no
environment-dependent DNS trickery, no `global.hostNodeIP` fallback.

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
  known constraint any customer-demo rehearsal must respect.

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

## Verification postmortem (2026-04-24)

The first end-to-end verification of the laptop-first chart — a
`helm upgrade` from the rev-36 (pre-ADR) chart to the rev-37 (post-ADR)
chart on a live cluster holding 133 interactions + 51 sessions — failed
on two chart-internal fault classes that had nothing to do with the
laptop-first IP work but that **the IP work's `--set` flag set was the
first command to surface**. Both are now fixed on this branch. Three
rules captured:

### PM-1 — `required` guards belong on every render-time secret

The MinIO StatefulSet used to render `MINIO_KMS_SECRET_KEY` inline as
`"audittrace-key:{{ .Values.secrets.minio.kmsKey }}"`. When `kmsKey`
was left blank — exactly what an operator who followed the bring-up
runbook verbatim did — the Pod spec contained `audittrace-key:`
(trailing colon, no key) and MinIO fatal-ed at startup with
`"kms: invalid key length 0"`. The failure mode was silent at
`helm template` time (no error) and loud at runtime (CrashLoopBackOff
+ a PVC-encryption alarm).

**Fix.** (a) Add a `required` guard in
`charts/audittrace/templates/secrets/secret-minio.yaml` on
`secrets.minio.secretKey` and `secrets.minio.kmsKey` so `helm template`
fails with an actionable message. (b) Move the KMS value off the
StatefulSet's `.env[].value` (plaintext in spec) and onto
`.env[].valueFrom.secretKeyRef` so the base64 key only ever lives in
the Secret. (c) Bake the `audittrace-key:` prefix inside the Secret's
stringData so consumers pull a ready-to-use value.

**Class.** Every other `secrets.*` required at runtime gets the same
audit on the next chart-wide pass. The rule: **if the chart renders a
secret value, the template must `required`-guard it or carry a
documented default. Silent `""` is never acceptable.**

### PM-2 — post-upgrade hooks that touch Postgres must wait for it

`audittrace-ensure-summariser-role` is a post-upgrade Helm hook that
runs `psql` against the in-cluster Postgres to re-ensure the dedicated
summariser role. During the failed rev-37 upgrade the
`audittrace-postgresql-0` StatefulSet also rolled (cause: an unrelated
spec-equivalent change triggered a rolling update). The hook started
in the gap between Postgres termination and the new pod becoming
Ready; its three allowed attempts all hit `connection refused` inside
~30 s; `BackoffLimitExceeded` failed the release.

**Fix.** (a) Add a `pg_isready` wait-loop at the top of the hook's
bash command (60 × 2 s = 120 s patience). (b) Raise `backoffLimit`
from 3 to 12 so the outer retry budget also tolerates a Postgres roll.

**Class.** Any future chart hook that depends on a rolled workload
gets the same pattern: wait explicitly for the dependency, don't
rely on the default backoffLimit.

### PM-3 — runbook verbatim-command is a contract

The Daily bring-up snippet in `docs/guides/zbook-runbook.md`
initially omitted every `secrets.*` flag. A first patch (commit
bb417d2) added six. The MinIO failure exposed that one was still
missing: `--set-file secrets.minio.kmsKey=secrets/minio_kms_key.txt`.
The chart now enforces the missing flag at `helm template` time, but
the runbook is what an operator reads; the two must match.

**Fix.** Add the seventh flag to the Daily bring-up snippet and a
callout explaining the committed-dev-key posture.

**Class.** Any change to the chart's `required` guards updates the
runbook's command set in the same commit. Runbook drift is treated as
a chart bug, not a docs bug.

### PM-4 — `nmcli networking off` is not airplane-mode simulation

Discovered during the first Phase 4 (airplane smoke) attempt on
2026-04-24. The rehearsal intent was "external network completely
gone, laptop-first stack still works" — a
`feedback_demo_rehearsal_non_home_network` gate for any upcoming
customer demo.

`sudo nmcli networking off` looked like the cleanest single-command
cut. It isn't: NetworkManager brings down *every* interface it knows
about, including ones it treats as "externally managed" — the k3s
flannel `cni0` bridge and the three Docker bridges
(`docker0`, `br-<compose>…`). The bridges come back `UP` when
`nmcli networking on` runs, but **without their IPs**, because NM
never re-applies the address that flannel / Docker installed out of
band.

Observed cascade during the 2026-04-24 incident:
- `cni0` stays UP but loses `10.42.0.1/24`. The pod-to-host path and
  the 10.42.0.0/24 connected route both vanish. Kubelet can't reach
  pod IPs on the sidecar's 15020/15021 ports; every pod's liveness
  and readiness probes start failing.
- The three Docker bridges lose their `172.17.0.1 / 172.18.0.1 /
  172.19.0.1` IPs. Docker-proxy listeners on the host keep
  `accept()`ing, but the forward leg to the container IP has no
  route, so every connection to `localhost:3000` / `:3001` / `:5000`
  resets.
- `memory-server` gets scheduled → tries to pull
  `localhost:5000/audittrace/memory-server:latest` → container-proxy
  forward fails → `ImagePullBackOff`.
- 30 min+ of chasing effects before the root cause (cni0/docker
  bridges both lost their IPs) surfaces.

**Fix / workaround (for operators):**
- To simulate airplane, use the *interface-level* cut that leaves
  flannel and Docker alone:
    ```
    sudo rfkill block wifi bluetooth
    sudo ip link set <ethN> down      # e.g. enxa0cec8afb44d
    ```
  `ip route show default` must return empty before testing.
- If you do land in the nmcli trap:
    ```
    sudo ip addr add 10.42.0.1/24 dev cni0
    sudo systemctl restart docker     # restores docker bridges + iptables
    kubectl -n audittrace delete pods -l app.kubernetes.io/component=memory-server
    ```
  k3s restart is a heavier alternative; `ip addr add` on cni0 plus
  a docker restart is faster.

**Class.** External interfaces only in any "off-LAN" simulation.
Never touch the management layer. The runbook's
§Airplane-mode smoke has a warning box repeating this rule;
`scripts/phase4-airplane-test.sh` aborts fast if `default route`
is not empty, which is the only reliable "the cut took" check.

## Amendment 2026-05-19 — FQDN-only (Rule 2 mechanism revised)

### Trigger

Designing the `aws-loadtest` substrate ([[adr-007-llm-runtime]],
[[adr-008-observability]]) exposed the fact that the IP-fallback
mechanism of Rule 2 does not extend to the cloud cleanly:

- AWS internal NLBs are addressed by their Route53 alias (a CNAME to
  the NLB's auto-generated AZ-bound A records), **not** by a stable
  IP. Forcing an IP into the chart means resolving the NLB at apply
  time and writing the resolved address into a Helm value, which is
  brittle (NLB IPs can change across maintenance windows).
- The headless `Service` + `Endpoints` shape with
  `addresses[].ip: <X>` that the chart used to render is a literal-IP
  shape by Kubernetes contract — `Endpoints` rejects hostnames.

The cloud-substrate shape ([[adr-007-llm-runtime]]) is therefore an
internal NLB with ACM-DNS-01 cert, fronted by a public-resolvable
FQDN `audittrace-loadtest-llm.allaboutdata.eu`. The chart must consume
that FQDN directly, not its resolved IP.

### What changed

| Surface | Before (≤ 2026-05-18) | After (≥ 2026-05-19) |
|---|---|---|
| Values shape | `global.hostNodeIP="10.42.0.1"`, `externalLLM.hostIP=""`, `observability.external.{langfuse,tempo,loki,prometheus}IP=""` | `externalLLM.host=""`, `observability.external.{langfuse,tempo,loki,prometheus}Host=""`. No `global.hostNodeIP` at all. |
| LLM Service templates | `kind: Service` (`clusterIP: None`) + `kind: Endpoints` (`addresses[].ip: <IP>`) | `kind: Service` (`type: ExternalName`, `spec.externalName: <FQDN>`) |
| Langfuse Service template | Same headless+Endpoints with IP fallback | `kind: Service` (`type: ExternalName`), gated on `langfuseHost` truthiness |
| OTel collector configmap | tempo endpoint via `tempoIP \| default hostNodeIP` | tempo endpoint via `tempoHost` (`required` when `otelCollector.enabled=true`) |
| Promtail configmap | loki url via `lokiIP \| default hostNodeIP` | loki url via `lokiHost` (`required` when `promtail.enabled=true`) |
| memory-server `AUDITTRACE_LANGFUSE_HOST` env | Constructed from `langfuseIP \| default hostNodeIP` | Emitted only when `langfuseHost` is set; absent otherwise |
| Local-dev DNS | Pod → host via flannel `cni0` bridge IP `10.42.0.1` | Pod → host via operator-provided FQDN; resolution via k3s CoreDNS `rewrite` plugin OR a sibling `audittrace-host.local` entry the operator installs on the host's resolver |

### Why this preserves Rule 1 (sibling stacks stay sibling)

Nothing about the sibling-compose posture changes. Langfuse and the
observability stack still run as independent docker-compose projects.
llama.cpp still runs as `systemd --user` on the host. Only the
mechanism by which the chart references those endpoints changes —
from "IP fallback" to "operator-resolved FQDN."

### Operator burden delta

The laptop operator now installs **one extra DNS primitive** before
`helm install`: an FQDN entry that resolves to the host running the
sibling stacks. The recommended laptop pattern:

1. Choose a sentinel FQDN such as `host.audittrace.local`.
2. Add a CoreDNS rewrite in k3s pointing it to the cni0 bridge IP
   (the same `10.42.0.1` that used to be the chart default), via a
   k3s CoreDNS `Corefile` snippet or a `NodeHosts` ConfigMap entry.
3. `helm install ... --set externalLLM.host=host.audittrace.local
   --set observability.external.langfuseHost=host.audittrace.local
   --set observability.external.tempoHost=host.audittrace.local
   --set observability.external.lokiHost=host.audittrace.local`.

The IP that used to live in the chart now lives once in CoreDNS,
where it belongs.

### Why the trade-off is positive

- **Cloud and laptop now use the same chart shape.** No IP-vs-FQDN
  branch in any template. The cloud uses Route53; the laptop uses a
  CoreDNS rewrite; both feed an `ExternalName` Service. Cargo cult
  removed: the chart no longer pretends to know how the operator
  resolves the FQDN.
- **No "auto-detect" script needed.** `scripts/detect-k3s-bridge.sh`
  is removed; CoreDNS does the indirection.
- **One fewer way to silently misconfigure.** A wrong IP in the old
  shape rendered a Service with a wrong `addresses[].ip` and failed
  at first connection. A wrong FQDN in the new shape fails at DNS
  lookup with a clean NXDOMAIN — diagnosis is `dig` instead of
  `tcpdump`.

### Verification gates (updated)

1. **Static grep:** `grep -rE '10\.42\.0\.1|192\.168\.1\.(100|231)|hostNodeIP|hostIP|langfuseIP|tempoIP|lokiIP'
   charts/ scripts/ Makefile .github/` returns empty.
2. **Default-render gate:** `helm template charts/audittrace
   --set externalLLM.host=... --set observability.external.langfuseHost=...
   --set observability.external.tempoHost=... --set observability.external.lokiHost=...`
   produces no `192.168.*` and no `10.42.*` strings (all four `--set`
   flags are required, by design — the chart fails fast without them).
3. **Render-without-FQDN gate:** `helm template charts/audittrace` (no
   FQDN flags) fails with four `required` errors naming the missing
   FQDN values — proving operators cannot silently install a broken
   chart.
4. **Airplane smoke** (unchanged from original Rule 2): laptop with
   external interfaces down still reaches the local CoreDNS-resolved
   FQDNs and produces a valid `/v1/chat/completions` response.

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
- PM-1 fix: `charts/audittrace/templates/secrets/secret-minio.yaml`,
  `charts/audittrace/templates/minio/statefulset.yaml`.
- PM-2 fix: `charts/audittrace/templates/postgres/job-summariser-role.yaml`.
- PM-3 fix: `docs/guides/zbook-runbook.md` §Daily bring-up.
