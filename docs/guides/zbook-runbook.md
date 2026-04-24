# Z-book runbook — laptop-first AuditTrace-AI

> From a fresh checkout to a working stack on your laptop, independent of
> upstream network state (home WiFi, phone hotspot, or airplane). Same
> install, same commands, same endpoints regardless. See ADR-045.

## Topology on one page

```
LAPTOP
├── Host processes (you start these)
│   ├── llama.cpp  :11435 (chat)  :11436 (embed)  :11437 (summarizer)
│   └── Docker sibling composes
│       ├── ~/work/langfuse/              → :3000
│       ├── ~/work/observability-stack/   → :4318 :14318 :3100 :19090 :3001
│       └── ~/work/librechat/  (when built)
│
└── k3s cluster (this chart)
    ├── Istio Gateway :443   → https://audittrace.local
    ├── memory-server, Postgres, Redis, Chroma, MinIO, Keycloak
    ├── OTel Collector DaemonSet → exports to sibling Tempo via cni0
    └── Promtail DaemonSet       → ships to sibling Loki via cni0
```

`cni0` is the k3s bridge interface; its IP (default `10.42.0.1`) is how
pods reach the laptop host regardless of wlan0 state.

## DNS / addressing rules

| Purpose | Mechanism | Address |
|---|---|---|
| Browser / CLI → Istio Gateway | `/etc/hosts` | `127.0.0.1  audittrace.local` |
| Pod → host (sibling stacks + llama.cpp) | k3s cni0 bridge | `10.42.0.1` (auto-detect) |
| Pod → pod (in-cluster services) | k3s CoreDNS | `*.audittrace.svc.cluster.local` |
| LibreChat container → Istio Gateway | Docker `extra_hosts` | `audittrace.local:host-gateway` |

Never hardcoded: `192.168.1.231`, `192.168.1.100`, `host.docker.internal`,
or any laptop-specific wlan IP.

## One-time laptop setup

### 1. `/etc/hosts`

```bash
grep -q 'audittrace\.local' /etc/hosts \
  || echo '127.0.0.1  audittrace.local' | sudo tee -a /etc/hosts
```

### 2. mkcert TLS

```bash
mkcert -install                                     # one-time CA trust
mkcert audittrace.local localhost 127.0.0.1 ::1     # produces tls.crt + tls.key
```

Firefox on Linux also needs `libnss3-tools` installed before `mkcert -install`
for the CA to land in Firefox's trust store.

### 3. llama.cpp as `systemd --user` units

Put the following under `~/.config/systemd/user/`. Adjust `--model` paths
to where you unpacked Qwen3.6-27B-Q4_K_M (`~/models/` per session memory).

`~/.config/systemd/user/llama-chat.service`:

```ini
[Unit]
Description=llama.cpp chat endpoint for AuditTrace-AI
After=default.target

[Service]
ExecStart=%h/bin/llama-server \
  --host 0.0.0.0 \
  --port 11435 \
  --model %h/models/Qwen_Qwen3.6-27B-Q4_K_M.gguf \
  --ctx-size 8192
Restart=on-failure

[Install]
WantedBy=default.target
```

Repeat for `llama-embed.service` (port 11436, embedding model) and
`llama-summarizer.service` (port 11437, summarizer model). Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now llama-chat llama-embed llama-summarizer
loginctl enable-linger $USER     # keep services alive after logout
```

Verify:

```bash
curl http://localhost:11435/v1/models
```

### 4. Sibling docker-compose stacks

These are independent repos under `~/work/`. Start each one once:

```bash
(cd ~/work/langfuse && docker compose up -d)
(cd ~/work/observability-stack && docker compose up -d)
```

**LibreChat** (added this week): when its compose lands at
`~/work/librechat/`, ensure its service has the following entry so the
container can reach the k3s Istio Gateway via the audittrace.local
hostname:

```yaml
services:
  librechat:
    extra_hosts:
      - "audittrace.local:host-gateway"
```

### 5. k3s install (one-time)

Follow the official `curl -sfL https://get.k3s.io | sh -` installer. After
install, ensure your user owns the kubeconfig:

```bash
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $USER: ~/.kube/config
chmod 600 ~/.kube/config
```

## Daily bring-up

From the repo root:

```bash
kubectl create ns audittrace --dry-run=client -o yaml | kubectl apply -f -

kubectl -n audittrace create secret tls audittrace-tls \
  --cert=tls.crt --key=tls.key --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install audittrace ./charts/audittrace \
  -n audittrace \
  --set global.hostNodeIP=$(./scripts/detect-k3s-bridge.sh) \
  --set secrets.postgres.password=test-pg-pass \
  --set secrets.postgres.appPassword=test-pg-pass \
  --set secrets.summariser.password=test-summariser-pw \
  --set secrets.chromadb.token=test-chroma-token \
  --set secrets.redis.password=test-redis-pass \
  --set secrets.minio.secretKey=test-minio-key \
  --set-file secrets.minio.kmsKey=secrets/minio_kms_key.txt
```

> The seven `secrets.*` values above are the repo-wide dev defaults (see
> the SECURITY NOTICE at the top of `charts/audittrace/values.yaml`).
> They are *only* safe for laptop / dev installs. Any production deploy
> sets `global.productionMode=true` and provisions these from a secret
> manager (ADR-040 Vault integration, target 2026-05-16).
>
> **`secrets.minio.kmsKey` MUST be passed** — the chart now fails at
> `helm template` time if it is blank, and historically (pre-2026-04-24)
> a blank value fatal-ed the MinIO pod with
> `"kms: invalid key length 0"` at runtime. `secrets/minio_kms_key.txt`
> is the committed dev key (44 bytes, including a trailing newline that
> the chart `trim`s). See ADR-045 §Verification postmortem.

Wait for Pods:

```bash
kubectl -n audittrace get pods -w
```

## Smoke tests

### Online smoke

```bash
curl -fk https://audittrace.local/health
./scripts/audittrace-login    # Device Flow → writes token to ~/.config
curl -fk -H "Authorization: Bearer $(cat ~/.config/audittrace/token)" \
  https://audittrace.local/v1/models
```

Browse to `http://localhost:3000` (Langfuse) — the completion trace should
appear with `user_id`, `session_id`, `trace_id` set.

### Airplane-mode smoke

```bash
sudo rfkill block all      # turn off WiFi / Bluetooth / cellular
sleep 3
ping -c1 -W1 8.8.8.8 && echo 'UNEXPECTED: still online'

# Run the exact same commands as "Online smoke" above. Everything should
# still work because no call path leaves the laptop.

sudo rfkill unblock all    # restore when done
```

Capture evidence to `tmp/evidence/zbook-$(date +%F).log` per
`feedback_test_and_evidence`.

## Overrides for the home-workstation case

If you want a Helm install to egress to the obs box at `192.168.1.231` and
the llama.cpp workstation at `192.168.1.100` instead of using the local
laptop bridge, override via `--set`:

```bash
helm upgrade --install audittrace ./charts/audittrace -n audittrace \
  --set global.hostNodeIP=192.168.1.231 \
  --set externalLLM.hostIP=192.168.1.100
```

The chart does not ship a `values-homelab.yaml`; the explicit `--set`
flags are the documented homelab pattern.

## Troubleshooting

- **`cni0` missing** → k3s isn't running, or uses a non-flannel CNI. Start
  k3s (`sudo systemctl start k3s`). `detect-k3s-bridge.sh` will fall back to
  `10.42.0.1` regardless.
- **Pods ImagePullBackOff `localhost:5000/...`** → start the local
  registry or switch `memoryServer.image.repository` to a pullable image.
- **Keycloak 502 via gateway** → gateway → keycloak pod path is in-cluster
  svc DNS; check `kubectl -n audittrace logs deploy/audittrace-keycloak`.
- **Langfuse trace not appearing** → sibling compose not up, or pod can't
  reach `10.42.0.1:3000`. Verify with
  `kubectl -n audittrace exec deploy/audittrace-memory-server -c memory-server -- curl -sI http://10.42.0.1:3000`.
- **OpenCode chat returns 502** → llama.cpp systemd unit not running;
  `systemctl --user status llama-chat`.
