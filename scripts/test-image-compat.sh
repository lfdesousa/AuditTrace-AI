#!/usr/bin/env bash
# test-image-compat.sh — run a candidate image against a captured PVC
# snapshot and verify it can read the data.
#
# Usage:
#   scripts/test-image-compat.sh <service> <candidate-image:tag> [<snapshot-dir>]
#
# Where:
#   <service>           : postgres | redis
#   <candidate-image>   : the docker image ref to test
#   <snapshot-dir>      : path to a snapshot (defaults to most-recent
#                         under ~/work/audittrace-private/data-snapshots/)
#
# Exit codes:
#   0 — candidate boots cleanly against the snapshot (PASS)
#   1 — candidate rejects the data with a known version-mismatch error
#   2 — environment problem (no docker, snapshot not found, etc.)
#
# Process:
#   1. Copy the snapshot to a scratch dir (postgres/redis WRITE to data
#      dir on startup; original snapshot stays untouched).
#   2. `docker compose run` the candidate via docker-compose.data-compat.yml.
#   3. Capture stdout/stderr for ~10s.
#   4. Grep for success markers + known failure markers.
#   5. Return verdict.
#
# Born 2026-05-13 from the chart-hardening incident — see
# `feedback_test_image_changes_locally_first` for the rule this
# script implements.

set -euo pipefail

SERVICE="${1:-}"
CANDIDATE="${2:-}"
SNAPSHOT_DIR="${3:-}"

if [[ -z "$SERVICE" || -z "$CANDIDATE" ]]; then
  cat <<USAGE >&2
usage: $0 <service> <candidate-image:tag> [<snapshot-dir>]

  service: postgres | redis
  candidate-image:tag: e.g. bitnamilegacy/redis:8.0.3-debian-12-r3
  snapshot-dir: defaults to the newest dir under
                ~/work/audittrace-private/data-snapshots/

examples:
  $0 redis bitnamilegacy/redis:8.0.3-debian-12-r3
  $0 redis localhost:5000/audittrace/redis:8.6.2-bitnami-frozen-apr17 \\
     ~/work/audittrace-private/data-snapshots/2026-05-13
USAGE
  exit 2
fi

# ─── env + paths ─────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.data-compat.yml"
SNAPSHOTS_ROOT="${AUDITTRACE_SNAPSHOTS_ROOT:-$HOME/work/audittrace-private/data-snapshots}"
SCRATCH_ROOT="${TEST_IMAGE_COMPAT_SCRATCH:-/tmp/audittrace-data-compat}"
WAIT_SECONDS="${WAIT_SECONDS:-10}"

log() { echo "[test-image-compat:$SERVICE] $*" >&2; }

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: $1 required but not on PATH" >&2
    exit 2
  }
}

require docker
[[ -f "$COMPOSE_FILE" ]] || {
  echo "error: $COMPOSE_FILE not found" >&2
  exit 2
}

# ─── resolve snapshot directory ──────────────────────────────────────────────

if [[ -z "$SNAPSHOT_DIR" ]]; then
  # Use the most-recent dated dir under $SNAPSHOTS_ROOT
  SNAPSHOT_DIR=$(find "$SNAPSHOTS_ROOT" -maxdepth 1 -type d -name '20*' 2>/dev/null \
                 | sort -r | head -1)
  [[ -n "$SNAPSHOT_DIR" ]] || {
    echo "error: no snapshot dirs under $SNAPSHOTS_ROOT — run snapshot-pvc.sh first" >&2
    exit 2
  }
  log "auto-selected snapshot: $SNAPSHOT_DIR"
fi

SERVICE_SNAP="${SNAPSHOT_DIR}/${SERVICE}"
[[ -d "$SERVICE_SNAP" ]] || {
  echo "error: $SERVICE_SNAP not found (expected snapshot subdir)" >&2
  exit 2
}

# ─── copy snapshot to scratch (DBs write to data dir on startup) ────────────

SCRATCH="${SCRATCH_ROOT}/${SERVICE}-$$"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"
cp -r "$SERVICE_SNAP/." "$SCRATCH/"
chmod -R 0777 "$SCRATCH"
# Bitnami images run as UID 1001. Postgres specifically refuses to
# start when the data dir's owner doesn't match (`FATAL: data
# directory ... has wrong ownership`) AND when the data dir is more
# permissive than 0750 (`FATAL: data directory ... has invalid
# permissions`). kubectl-cp'd files come out as the host user +
# default permissive permissions, so we chown + chmod via a
# throwaway root container.
if [[ "$SERVICE" == "postgres" ]]; then
  docker run --rm -v "$SCRATCH:/scratch" alpine:3.20 sh -c '
    chown -R 1001:1001 /scratch
    chmod 0700 /scratch/data
    chmod 0700 /scratch
  ' >/dev/null 2>&1 || {
    echo "warning: chown+chmod prep failed for postgres" >&2
  }
else
  docker run --rm -v "$SCRATCH:/scratch" alpine:3.20 \
    chown -R 1001:1001 /scratch >/dev/null 2>&1 || true
fi
log "snapshot copied to scratch: $SCRATCH"

# ─── service-specific success + failure markers ──────────────────────────────

case "$SERVICE" in
  postgres)
    ENV_VAR="PG_CANDIDATE_IMAGE"
    SNAP_ENV_VAR="PG_SNAPSHOT_DIR"
    SUCCESS_REGEX='database system is ready to accept connections'
    FAILURE_REGEX="FATAL:[[:space:]]+database files are incompatible|FATAL:[[:space:]]+database files are too|version of pg_control"
    ;;
  redis)
    ENV_VAR="REDIS_CANDIDATE_IMAGE"
    SNAP_ENV_VAR="REDIS_SNAPSHOT_DIR"
    SUCCESS_REGEX='Ready to accept connections'
    FAILURE_REGEX="Can't handle RDB format|Error reading the RDB|AOF loading aborted"
    ;;
  *)
    echo "error: unknown service '$SERVICE'" >&2
    exit 2
    ;;
esac

# ─── run the candidate ───────────────────────────────────────────────────────

LOG_FILE="${SCRATCH}.log"
log "running candidate: $CANDIDATE"
log "  data dir: $SCRATCH"
log "  log:      $LOG_FILE"

# `compose run` is sync; pipe to log file; kill after $WAIT_SECONDS.
# `--rm` + `--no-TTY` keep it scriptable.
(
  cd "$REPO_ROOT"
  export "$ENV_VAR=$CANDIDATE"
  export "$SNAP_ENV_VAR=$SCRATCH"
  docker compose -f "$COMPOSE_FILE" run --rm --no-TTY "${SERVICE}-candidate" \
    > "$LOG_FILE" 2>&1 &
  PROC=$!
  sleep "$WAIT_SECONDS"
  kill -INT "$PROC" 2>/dev/null || true
  wait "$PROC" 2>/dev/null || true
)

# ─── verdict ─────────────────────────────────────────────────────────────────

echo
echo "── last 25 log lines ──" >&2
tail -25 "$LOG_FILE" >&2
echo "────" >&2

VERDICT="?"
if grep -qE "$FAILURE_REGEX" "$LOG_FILE"; then
  VERDICT="FAIL"
elif grep -qE "$SUCCESS_REGEX" "$LOG_FILE"; then
  VERDICT="PASS"
fi

case "$VERDICT" in
  PASS)
    log "✅ PASS — candidate reads the snapshot cleanly"
    log "   log saved: $LOG_FILE"
    log "   scratch:   $SCRATCH (delete with: rm -rf $SCRATCH)"
    exit 0
    ;;
  FAIL)
    log "❌ FAIL — candidate rejected the snapshot (version-incompat marker matched)"
    log "   log saved: $LOG_FILE"
    log "   scratch:   $SCRATCH"
    exit 1
    ;;
  *)
    log "❓ INCONCLUSIVE — neither success nor failure marker found in ${WAIT_SECONDS}s."
    log "   Inspect: $LOG_FILE"
    log "   Bump WAIT_SECONDS or check the log for unexpected startup paths."
    exit 1
    ;;
esac
