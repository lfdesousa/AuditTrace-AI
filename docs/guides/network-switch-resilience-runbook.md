# Network-switch resilience runbook (SPEC #443)

Apply + rollback + validation procedure for "mesh survives a wifi<->wired
network switch" (SPEC #443, the #307 family). All steps below are
**operator-gated** (need `sudo` and a human at the machine) — the fleet
builds, tests, and documents these artefacts; it never applies them
(SPEC #443 s6, mirroring the tag-push human-gate discipline).

## Why this exists

Switching the laptop between wifi and wired reliably took the whole
AuditTrace mesh down until a manual `sudo systemctl restart k3s`. Root
cause (SPEC #443 s2, evidenced 2026-08-09): with no
`/etc/rancher/k3s/config.yaml`, k3s auto-detects its node IP from the
default-route interface. `ip route get 10.43.0.1` (the kube-API ClusterIP)
egresses whichever NIC currently holds the default route — switch NICs,
and the pod->ClusterIP path breaks, istiod goes `Unauthenticated`
mesh-wide (the exact `no route to host` signature runbook 16 already
fixed for `flannel.1`/`cni0`, but that fix does not cover the node-IP/route
binding itself).

The fix pins the node IP to a persistent `k3s0` dummy interface
(`10.10.10.1/32`) outside the pod (`10.42.0.0/16`), service
(`10.43.0.0/16`), and LAN (`192.168.1.0/24`) ranges, so the cluster
network no longer depends on which physical NIC is active.

## Prerequisites

- Root on the host (`sudo`).
- A clean checkout of this repo at `/home/lfdesousa/work/AuditTrace-AI`
  (the host units below hardcode this path, matching the existing
  `audittrace-vault-auto-unseal.service` / `audittrace-pod-reaper.service`
  convention for host-level, non-chart-shipped tooling).
- `kubectl` configured against the cluster.

## Step 1 — install the persistent k3s0 dummy interface

```bash
cd /home/lfdesousa/work/AuditTrace-AI

sudo cp scripts/network/audittrace-k3s0-dummy-up.sh /usr/local/sbin/
sudo chmod 0755 /usr/local/sbin/audittrace-k3s0-dummy-up.sh

sudo cp scripts/network/audittrace-k3s0-dummy.service.template \
        /etc/systemd/system/audittrace-k3s0-dummy.service
sudo systemctl daemon-reload
sudo systemctl enable --now audittrace-k3s0-dummy.service

# verify
ip -4 addr show k3s0        # want: inet 10.10.10.1/32
systemctl is-active audittrace-k3s0-dummy.service   # want: active
```

## Step 2 — exclude k3s0 from NetworkManager

k3s0 is created by the plain systemd unit above, **not** an NM connection
profile (a `type=dummy` NM connection and `unmanaged-devices` for the same
interface name are mutually exclusive — see
`scripts/network/99-audittrace-k3s0-unmanaged.conf.template`'s header for
why). This mirrors the existing #307 fix for `flannel.1`/`cni0` exactly:
create externally, then tell NM to never touch it.

```bash
sudo cp scripts/network/99-audittrace-k3s0-unmanaged.conf.template \
        /etc/NetworkManager/conf.d/99-audittrace-k3s0-unmanaged.conf
sudo nmcli general reload   # non-disruptive; does not drop active connections

# verify
nmcli -t -f DEVICE,STATE device status | grep k3s0   # want: k3s0:unmanaged
```

`flannel.1`/`cni0`/`veth*`/`cali*` are **already** covered by the existing
`/etc/NetworkManager/conf.d/99-audittrace-k3s-unmanaged.conf` (runbook 16)
— no change needed there.

## Step 3 — pin k3s to k3s0

```bash
sudo cp scripts/network/k3s-config.yaml.template /etc/rancher/k3s/config.yaml
cat /etc/rancher/k3s/config.yaml   # sanity-read before restarting anything
```

Confirm `tls-san` carries **both** the new `10.10.10.1` entry **and** every
existing SAN (`audittrace.local`, `127.0.0.1`) — dropping an existing SAN
here breaks TLS for every current client (SPEC #443 s7).

## Step 4 — the one-time k3s restart to adopt

```bash
sudo systemctl restart k3s
kubectl get node -o wide   # INTERNAL-IP must now read 10.10.10.1
```

This is the **only** planned k3s restart in this whole procedure — after
this, a wifi<->wired switch should never need one again (that is the
acceptance criterion this fix exists to satisfy).

## Step 5 — install the auto-heal dispatcher hook (defense-in-depth)

```bash
sudo cp scripts/network/NN-k3s-clusterip-guard \
        /etc/NetworkManager/dispatcher.d/90-k3s-clusterip-guard
sudo chmod 0755 /etc/NetworkManager/dispatcher.d/90-k3s-clusterip-guard
```

No further action needed — NM picks up dispatcher.d scripts automatically
on the next `up`/`down`/`connectivity-change` event; no NM restart required.

## Step 6 — validate with the neuterable guard

```bash
cd /home/lfdesousa/work/AuditTrace-AI
.venv/bin/python scripts/network/verify_clusterip_resilience.py --insecure
```

Expect all four checks to PASS:

```
[PASS] route-via-dummy-iface: ClusterIP path egresses 'k3s0'
[PASS] node-internal-ip-pinned: node InternalIP == '10.10.10.1'
[PASS] istiod-reachable-no-route: istiod reachable, 0 smoking-gun lines in the last 2m
[PASS] front-door-health: front-door /health reports status=ok
RESULT: healthy (ClusterIP path is k3s0-bound)
```

A non-zero exit / any `[FAIL]` line means the fix did not take — do not
proceed to Step 7 until this is clean.

## Step 7 — the physical validation (SPEC #443 acceptance criteria 4.1-4.3)

With the mesh healthy on (say) wifi:

1. Note the current state: `ip route get 10.43.0.1` (should show `dev k3s0`),
   `kubectl get node -o wide` (INTERNAL-IP `10.10.10.1`).
2. **Physically switch to wired** (plug the cable in, or the reverse —
   whichever direction is untested).
3. Wait ~10s for NM to settle, then re-run:
   ```bash
   .venv/bin/python scripts/network/verify_clusterip_resilience.py --insecure
   ```
   Expect all four checks to still PASS — **no** `systemctl restart k3s`.
4. Confirm istiod's own logs independently:
   ```bash
   export KUBECONFIG=$HOME/.kube/config
   kubectl -n istio-system logs deploy/istiod --since=2m \
     | grep -icE 'no route to host|Unauthenticated|tokenreviews'   # want: 0
   ```
5. Confirm the front door end-to-end (a real recall E2E, not just `/health`):
   see `docs/guides/deployment-runbook.md`'s verify section for the
   `POST /v1/chat/completions` smoke test.
6. Switch back (wired -> wifi) and repeat steps 3-5 in the other direction.

Both directions passing, with zero manual `systemctl restart k3s`, is the
SPEC #443 acceptance bar.

## Rollback

```bash
sudo rm -f /etc/rancher/k3s/config.yaml
sudo systemctl restart k3s

sudo systemctl disable --now audittrace-k3s0-dummy.service
sudo rm -f /etc/systemd/system/audittrace-k3s0-dummy.service
sudo systemctl daemon-reload

sudo rm -f /etc/NetworkManager/conf.d/99-audittrace-k3s0-unmanaged.conf
sudo rm -f /etc/NetworkManager/dispatcher.d/90-k3s-clusterip-guard
sudo nmcli general reload

ip link delete k3s0   # if it still exists
```

This reverts to the pre-fix, auto-detected-node-IP behaviour — the
#307-class exposure this runbook exists to close returns. Roll forward
only in normal operation ([[feedback_no_image_rollback_across_migration]]);
this rollback is for a bad apply, not a routine escape hatch.

## Idempotency + reboot survival (SPEC #443 acceptance criterion 5)

- Re-running `audittrace-k3s0-dummy-up.sh` (directly, via the systemd unit,
  or via the dispatcher hook's repair path) is a clean no-op when k3s0 is
  already correctly configured — every step checks current state first.
- Re-copying any of the `.template` files and re-running `nmcli general
  reload` / `systemctl restart k3s` is likewise a no-op on an
  already-correct host.
- A reboot re-creates k3s0 via `audittrace-k3s0-dummy.service`
  (`Before=k3s.service`, enabled), re-applies the NM exclusion (a static
  conf.d file, read at every NetworkManager start), and k3s reads the same
  `/etc/rancher/k3s/config.yaml` — the fix survives a reboot without any
  operator action, closing the #307 tail item ("provision on host rebuild").

## Related artefacts

| Artefact | Role |
|---|---|
| `scripts/network/verify_clusterip_resilience.py` | The neuterable guard — SPEC #443's falsifiable acceptance check. |
| `scripts/network/audittrace-k3s0-dummy-up.sh` + `.service.template` | Creates + persists the pinned k3s0 dummy interface. |
| `scripts/network/99-audittrace-k3s0-unmanaged.conf.template` | Excludes k3s0 from NetworkManager (mirrors runbook 16's flannel.1/cni0 fix). |
| `scripts/network/k3s-config.yaml.template` | Pins k3s's node-ip/flannel-iface/tls-san. |
| `scripts/network/NN-k3s-clusterip-guard` | NetworkManager dispatcher auto-heal (defense-in-depth). |
| `~/work/audittrace-private/runbooks/16-networkmanager-flannel-permanent-fix-307.md` | The prior #307 fix (flannel.1/cni0 unmanaged) this one extends. |
| `~/work/audittrace-private/runbooks/13-k3s-laptop-power-cycle-recovery.md` | Recovery runbook if a residual flap ever slips through both layers. |
