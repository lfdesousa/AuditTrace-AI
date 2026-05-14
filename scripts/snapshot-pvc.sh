#!/usr/bin/env bash
# snapshot-pvc.sh — capture a binary PVC snapshot for the data-compat harness
#
# Usage:
#   scripts/snapshot-pvc.sh <service> <output-dir>
#
# Where <service> is one of: postgres, redis
#       <output-dir> is the host directory to write the snapshot into
#
# Process:
#   1. Scale the StatefulSet to 0 replicas (frees the RWO PVC).
#   2. Launch a busybox probe pod that mounts the PVC read-only.
#   3. `kubectl cp` the data dir contents to <output-dir>/<service>/.
#   4. Delete the probe pod.
#   5. Scale the StatefulSet back to 1.
#
# IMPORTANT — this temporarily takes the service offline (~10-30s
# depending on snapshot size). On production, run during a quiet
# window or pre-deploy when the service can be drained.
#
# Born 2026-05-13 from the chart-hardening incident:
#   feedback_test_image_changes_locally_first
#   project_followup_data_compat_docker_compose
#
# Why a CLI script (not a Kubernetes Job): the operator needs to
# inspect the captured data locally and replay it through
# `test-image-compat.sh`. A Job would write the snapshot to MinIO/PVC
# which is one indirection too many for the validate-then-deploy loop.

set -euo pipefail

SERVICE="${1:-}"
OUT_DIR="${2:-}"

if [[ -z "$SERVICE" || -z "$OUT_DIR" ]]; then
  cat <<USAGE >&2
usage: $0 <service> <output-dir>

  service: postgres | redis
  output-dir: absolute path on the host (e.g. ~/work/audittrace-private/data-snapshots/2026-05-13)

example:
  $0 postgres ~/work/audittrace-private/data-snapshots/$(date +%Y-%m-%d)
USAGE
  exit 2
fi

NAMESPACE="${AUDITTRACE_NAMESPACE:-audittrace}"

case "$SERVICE" in
  postgres)
    STS="audittrace-postgresql"
    PVC="data-audittrace-postgresql-0"
    MOUNT_PATH="/bitnami/postgresql/data"
    ;;
  redis)
    STS="audittrace-redis-master"
    PVC="redis-data-audittrace-redis-master-0"
    MOUNT_PATH="/bitnami/redis/data"
    ;;
  *)
    echo "error: unknown service '$SERVICE' (postgres|redis)" >&2
    exit 2
    ;;
esac

PROBE_POD="snapshot-probe-${SERVICE}"
DEST="${OUT_DIR}/${SERVICE}"

log() { echo "[snapshot-pvc:$SERVICE] $*" >&2; }

require() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "error: $cmd required but not found" >&2
    exit 1
  }
}

require kubectl
require tar

mkdir -p "$DEST"

log "scaling $STS to 0 replicas (will restore after capture)..."
kubectl -n "$NAMESPACE" scale sts "$STS" --replicas=0 >/dev/null
# Wait for the pod to actually clear so the PVC is released.
for i in 1 2 3 4 5 6 7 8 9 10; do
  if ! kubectl -n "$NAMESPACE" get pod "${STS}-0" >/dev/null 2>&1; then
    log "  pod cleared after ${i}s"
    break
  fi
  sleep 1
done

log "launching probe pod with PVC RO-mounted..."
kubectl -n "$NAMESPACE" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${PROBE_POD}
  namespace: ${NAMESPACE}
  annotations:
    sidecar.istio.io/inject: "false"
spec:
  restartPolicy: Never
  containers:
  - name: probe
    image: busybox:1.36
    command: ["sleep", "600"]
    volumeMounts:
    - name: data
      mountPath: ${MOUNT_PATH}
      readOnly: true
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: ${PVC}
      readOnly: true
YAML

kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${PROBE_POD}" --timeout=60s >/dev/null

log "kubectl cp ${MOUNT_PATH} → ${DEST} ..."
kubectl -n "$NAMESPACE" cp "${PROBE_POD}:${MOUNT_PATH}" "${DEST}"

log "deleting probe pod..."
kubectl -n "$NAMESPACE" delete pod "${PROBE_POD}" --grace-period=0 --force >/dev/null 2>&1 || true

log "scaling $STS back to 1 replica..."
kubectl -n "$NAMESPACE" scale sts "$STS" --replicas=1 >/dev/null

log "waiting for $STS-0 Ready..."
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${STS}-0" --timeout=300s >/dev/null || {
  echo "warning: ${STS}-0 did not become Ready in 5m; check kubectl get pods" >&2
}

log "snapshot complete:"
ls -la "${DEST}" | head -20 >&2
du -sh "${DEST}" >&2

echo "${DEST}"
