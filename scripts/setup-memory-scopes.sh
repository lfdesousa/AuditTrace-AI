#!/usr/bin/env bash
# Operator-run idempotent script — provisions the three memory-layer
# write scopes on the audittrace realm via kcadm.sh, when (e.g.) the
# post-install/post-upgrade Helm Job is unavailable or the operator
# wants to reconcile the realm without re-running `helm upgrade`.
#
# 99% of the time the chart's `ensure-memory-scopes` Job (in
# `templates/keycloak/job-memory-scopes.yaml`) does this automatically
# on every helm install/upgrade, so this script is a backstop —
# useful for:
#   - bare-metal disaster recovery (realm wiped + re-imported)
#   - debugging the kcadm logic without bouncing the chart
#   - manual re-provisioning after a `vault.enabled=false` ↔ true
#     migration where the Job's Vault role wasn't yet ready
#
# Pre-requisites:
#   1. `kubectl` configured for the audittrace cluster.
#   2. The audittrace Keycloak pod is running.
#   3. KEYCLOAK_ADMIN_PASSWORD reachable in env, OR vault.enabled=true
#      and the operator can read kv/audittrace/keycloak/admin via
#      `vault kv get`.
#
# Usage (env-var creds):
#   KEYCLOAK_ADMIN_PASSWORD=...  ./scripts/setup-memory-scopes.sh
#
# Usage (Vault-resolved creds):
#   vault login -method=...
#   ./scripts/setup-memory-scopes.sh         # script reads from vault
#
# Idempotent: every operation gates on a "does it already exist?"
# check, mirroring the Helm Job's bash logic verbatim. Re-runs are
# safe (and recommended after any realm-import).

set -euo pipefail

# Duplicate the REAL stdout/stderr to fd 3/4 BEFORE any later command
# in this script redirects fd 1/2 of its own invocation (e.g. every
# `acl_e2e_kc ... >/dev/null` call). Without this, a TERM/timeout that
# strikes WHILE such a redirected call is in flight would have the
# trap's own `acl-e2e:` line inherit that call's CURRENT fd 1 (the
# `/dev/null` it was redirected to), silently losing the stdout half
# of "exactly once on both streams" — found by this WU's own SIGTERM
# test against the sunset-delete call. fd 3/4 are independent
# duplicates of the ORIGINAL pipe/terminal and are never themselves
# redirected, so acl_e2e_emit always reaches the real streams.
exec 3>&1 4>&2

# ----- ACL WU-2c-A0: named-failure-line diagnostics (F/F1/F2/F3) -----
# Every exit path of this script — an explicit refusal, an implicit
# `set -e`/`-u`/`pipefail` exit, a Job SIGTERM/SIGKILL landing on a
# stuck kcadm call, or a kcadm call that times out — prints exactly
# one line of the form
#   acl-e2e: code=<code> stage=<stage> rc=<rc> line=<n|?> cmd=<masked|-> detail=<non-secret>
# to BOTH stdout and stderr before the process exits (ACL WU-2c
# Addendum F/F1/F2/F3). `cmd=` is `$BASH_COMMAND`
# (source text, never an expanded value) with every password token
# masked; `detail=` is always a closed-set reason/count/path — never a
# kcadm response body (which may carry `registrationAccessToken`) and
# never a raw credential.
#
# Bootstrap tool checks run with NO trap installed yet (nothing would
# catch their own failure) — each prints the line itself and exits.
if ! command -v timeout >/dev/null 2>&1; then
  printf 'acl-e2e: code=timeout-missing stage=pre-step:init rc=127 line=? cmd=- detail=coreutils-timeout-not-on-PATH\n' >&3
  printf 'acl-e2e: code=timeout-missing stage=pre-step:init rc=127 line=? cmd=- detail=coreutils-timeout-not-on-PATH\n' >&4
  exit 1
fi
if ! command -v mktemp >/dev/null 2>&1; then
  printf 'acl-e2e: code=mktemp-missing stage=pre-step:init rc=127 line=? cmd=- detail=coreutils-mktemp-not-on-PATH\n' >&3
  printf 'acl-e2e: code=mktemp-missing stage=pre-step:init rc=127 line=? cmd=- detail=coreutils-mktemp-not-on-PATH\n' >&4
  exit 1
fi

ACL_E2E_REPORTED=0
ACL_E2E_CHILD=""
ACL_E2E_STAGE="pre-step:init"
ACL_E2E_ERR_LINE="?"
ACL_E2E_ERR_CMD="-"
ACL_E2E_REPORT_FILE="$(mktemp)"
unset ACL_E2E_VIA_KC ACL_E2E_CALL_LINE || true

# `$BASH_COMMAND` is source text, never an expanded value — masking the
# variable NAME here is defence in depth only; the real leak vectors
# (`set -x`, `printenv`, `declare -p`, `cat <<<"$KEYCLOAK_ADMIN_PASSWORD"`)
# are forbidden outright by the secret-hygiene test (AC-F2, F3 SF3-5).
acl_e2e_mask() {
  local s="$1"
  s="${s//KEYCLOAK_ADMIN_PASSWORD/<redacted-var>}"
  # Collapse any embedded CR/LF (a malformed multi-line kcadm response
  # read naively into a value) so it can never split the one-line
  # emission across terminal lines, THEN fail closed on anything
  # token-shaped (>=3 dot-separated base64url segments, >=15 chars
  # each) that might have ridden in via a response-derived value (F
  # section 1.2: "detail= never carries a kcadm response body"; R4).
  s="${s//$'\r'/ }"
  s="${s//$'\n'/ }"
  while [[ "${s}" =~ ([A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}) ]]; do
    s="${s/${BASH_REMATCH[1]}/<redacted-token>}"
  done
  printf '%s' "${s}"
}

# The ONE line, both streams, one shape (F2 SF-1).
acl_e2e_emit() {
  local code="$1" rc="$2" line="$3" cmd="$4" detail="$5" msg
  msg="acl-e2e: code=${code} stage=${ACL_E2E_STAGE} rc=${rc} line=${line} cmd=$(acl_e2e_mask "${cmd}") detail=$(acl_e2e_mask "${detail}")"
  printf '%s\n' "${msg}" >&3
  printf '%s\n' "${msg}" >&4
}

# Preferred-cause resolver shared by the ERR/EXIT/TERM/INT paths: an
# inner, more specific cause recorded by a nested subshell's
# `acl_e2e_fail` always wins over a generic fallback (F2 BF1-b).
acl_e2e_report() {
  local fallback_code="$1" rc="$2" fallback_detail="$3"
  [[ "${ACL_E2E_REPORTED}" -eq 1 ]] && return 0
  if [[ -s "${ACL_E2E_REPORT_FILE}" ]]; then
    local inner icode irc idetail
    inner="$(cat "${ACL_E2E_REPORT_FILE}" 2>/dev/null || true)"
    icode="${inner#code=}"; icode="${icode%% stage=*}"
    irc="${inner#*rc=}"; irc="${irc%% detail=*}"
    idetail="${inner#*detail=}"
    acl_e2e_emit "${icode}" "${irc}" "${ACL_E2E_ERR_LINE}" "${ACL_E2E_ERR_CMD}" "${idetail}"
  else
    acl_e2e_emit "${fallback_code}" "${rc}" "${ACL_E2E_ERR_LINE}" "${ACL_E2E_ERR_CMD}" "${fallback_detail}"
  fi
  ACL_E2E_REPORTED=1
}

# Explicit refusal — every `|| acl_e2e_fail <code> "<detail>"` site in
# this script. Inside a subshell ($( ), a pipeline element, `( )`) this
# cannot set this shell's ACL_E2E_REPORTED, so it records into the
# parent-owned report file instead (an EXIT trap does NOT itself run
# inside a `$( )`/`( )`/pipeline subshell on bash 5.1/5.2 — measured;
# no `trap - EXIT` is needed to suppress a second print there) so the
# one real top-level `|| acl_e2e_fail` prints the ONE line (F2 BF1-b;
# AC-F14 pins that every call site sits where this holds).
#
# The real rc of the FAILING command the `||` just reacted to is still
# live in $? when this function is entered — captured as the FIRST
# statement, before any other command (even `local`) would overwrite
# it. A timeout (124) or a delivered signal's own exit status (143/130,
# on the rare top-level call where the trap itself never got to run
# first) always overrides the caller's generic code, so a guard can
# never collapse a `kcadm-timeout`/`signal-term`/`signal-int` into its
# own more general refusal code (R3).
acl_e2e_fail() {
  local __acl_rc=$?
  local code="$1"
  shift
  local detail="$*"
  case "${__acl_rc}" in
    124) code="kcadm-timeout" ;;
    143) code="signal-term" ;;
    130) code="signal-int" ;;
    97) code="unwrapped-call" ;;
    *) __acl_rc=1 ;;
  esac
  if [[ "${BASH_SUBSHELL}" -gt 0 ]]; then
    printf 'code=%s stage=%s rc=%s detail=%s' "${code}" "${ACL_E2E_STAGE}" "${__acl_rc}" "${detail}" > "${ACL_E2E_REPORT_FILE}"
    exit "${__acl_rc}"
  fi
  acl_e2e_report "${code}" "${__acl_rc}" "${detail}"
  exit "${__acl_rc}"
}

acl_e2e_on_err() {
  ACL_E2E_ERR_LINE="${BASH_LINENO[0]}"
  ACL_E2E_ERR_CMD="${BASH_COMMAND}"
}

acl_e2e_on_exit() {
  local rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    local code="unguarded-failure"
    case "${rc}" in
      124) code="kcadm-timeout" ;;
      97) code="unwrapped-call" ;;
    esac
    acl_e2e_report "${code}" "${rc}" "unreported-exit"
  fi
  rm -f "${ACL_E2E_REPORT_FILE}" 2>/dev/null || true
}

acl_e2e_on_signal() {
  local sig="$1" rc=143
  [[ "${sig}" == "INT" ]] && rc=130
  [[ -n "${ACL_E2E_CHILD}" ]] && kill "${ACL_E2E_CHILD}" 2>/dev/null || true
  acl_e2e_report "signal-${sig,,}" "${rc}" "killed-by-SIG${sig}"
  rm -f "${ACL_E2E_REPORT_FILE}" 2>/dev/null || true
  exit "${rc}"
}

trap 'acl_e2e_on_err' ERR
trap 'acl_e2e_on_exit' EXIT
trap 'acl_e2e_on_signal TERM' TERM
trap 'acl_e2e_on_signal INT' INT
set -E

NAMESPACE="${AUDITTRACE_NAMESPACE:-audittrace}"
RELEASE="${AUDITTRACE_RELEASE:-audittrace}"
REALM="${REALM:-audittrace}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "❌ Required command not on PATH: $1" >&2
    exit 1
  fi
}
require_cmd kubectl

# ----- Resolve Keycloak admin password -----
# Three resolution paths, tried in order:
#   1. KEYCLOAK_ADMIN_PASSWORD env var already set — use as-is.
#   2. Local `vault` CLI on PATH (operator has vault installed) — `vault kv get`.
#   3. In-cluster fallback: `kubectl exec audittrace-vault-0 -- vault kv get`.
#      Mirrors setup-vault.sh's vault_exec pattern so the umbrella target
#      `make k8s-bootstrap-secrets` works without requiring the vault CLI on
#      the operator's machine — the only prerequisite is VAULT_TOKEN exported,
#      same as setup-vault.sh.
if [[ -z "${KEYCLOAK_ADMIN_PASSWORD:-}" ]]; then
  if command -v vault >/dev/null 2>&1; then
    echo "▶ KEYCLOAK_ADMIN_PASSWORD not set — attempting Vault lookup (local CLI)..."
    if KEYCLOAK_ADMIN_PASSWORD=$(vault kv get -field=password kv/audittrace/keycloak/admin 2>/dev/null); then
      export KEYCLOAK_ADMIN_PASSWORD
      echo "  ✓ resolved from kv/audittrace/keycloak/admin"
    else
      echo "❌ Could not resolve KEYCLOAK_ADMIN_PASSWORD via local Vault CLI." >&2
      echo "   Either set the env var directly or run \`vault login\` first." >&2
      exit 1
    fi
  elif [[ -n "${VAULT_TOKEN:-}" ]] \
       && kubectl -n "${NAMESPACE}" get pod "${RELEASE}-vault-0" >/dev/null 2>&1; then
    echo "▶ KEYCLOAK_ADMIN_PASSWORD not set — attempting in-cluster Vault lookup..."
    if KEYCLOAK_ADMIN_PASSWORD=$(kubectl -n "${NAMESPACE}" exec -i "${RELEASE}-vault-0" -- \
                                   env "VAULT_TOKEN=${VAULT_TOKEN}" \
                                   vault kv get -field=password kv/audittrace/keycloak/admin 2>/dev/null); then
      export KEYCLOAK_ADMIN_PASSWORD
      echo "  ✓ resolved from kv/audittrace/keycloak/admin (via kubectl exec ${RELEASE}-vault-0)"
    else
      echo "❌ In-cluster Vault lookup failed. Has 'vault kv put kv/audittrace/keycloak/admin password=...' been run?" >&2
      exit 1
    fi
  else
    echo "❌ KEYCLOAK_ADMIN_PASSWORD env var is empty and no Vault path is available." >&2
    echo "   Either:" >&2
    echo "     - export KEYCLOAK_ADMIN_PASSWORD directly, or" >&2
    echo "     - install the vault CLI locally and run \`vault login\`, or" >&2
    echo "     - export VAULT_TOKEN so this script can read via kubectl exec." >&2
    exit 1
  fi
fi

# ----- Find the Keycloak pod -----
KC_POD="$(kubectl -n "${NAMESPACE}" get pod \
            -l app.kubernetes.io/component=keycloak \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
if [[ -z "${KC_POD}" ]]; then
  echo "❌ No Keycloak pod found in namespace ${NAMESPACE}." >&2
  exit 1
fi

echo "🔐 setup-memory-scopes.sh — reconciling memory:*:write scopes"
echo "   namespace=${NAMESPACE} release=${RELEASE} realm=${REALM} pod=${KC_POD}"

# acl-e2e-kc-begin
# Every kcadm invocation in this script goes through this wrapper
# (F2 BF1-a) — nothing else in either site calls kcadm.sh directly.
# Interruptible (background + `wait` — a trapped signal interrupts
# `wait`, F1 §1.2) and bounded (`timeout -k 5 <default 20>`, F2 S-b:
# for the many invocations nested inside a `$(...)` command
# substitution this per-call timeout is the ONLY mechanism, since a
# signal to the main process is deferred until that foreground
# subshell returns). The `ACL_E2E_VIA_KC`/`ACL_E2E_CALL_LINE`
# environment prefix on this one command (never `export`) is the
# dynamic primary proof that every call on an exercised path goes
# through here (F3 §1.1): a harness fake invoked without the marker
# fails loudly instead of passing silently.
acl_e2e_kc() {
  local rc
  ACL_E2E_VIA_KC=1 ACL_E2E_CALL_LINE="${BASH_LINENO[0]}" \
    timeout -k 5 "${ACL_E2E_KCADM_TIMEOUT:-20}" kubectl -n "${NAMESPACE}" exec -i "${KC_POD}" -c keycloak -- /opt/keycloak/bin/kcadm.sh "$@" &
  ACL_E2E_CHILD=$!
  wait "${ACL_E2E_CHILD}"
  rc=$?
  ACL_E2E_CHILD=""
  return "${rc}"
}
# acl-e2e-kc-end

# ----- Authenticate -----
ACL_E2E_STAGE="pre-step:auth"
echo "▶ authenticating to Keycloak admin..."
acl_e2e_kc config credentials \
  --server http://localhost:8080 \
  --realm master \
  --user admin \
  --password "${KEYCLOAK_ADMIN_PASSWORD}" >/dev/null
echo "  ✓ authenticated"
ACL_E2E_STAGE="pre-step:init"

# #370 RATIFIED 2026-08-11 — this array (bound to `admin-client` as a
# DEFAULT scope below, via CLIENT_KIND) is WHY the live realm's
# `admin-client` carries more default scopes than
# `keycloak/realm-audittrace.json` declares for it. That JSON-vs-live
# delta is intentional, not drift: `admin-client` is meant to hold
# every corpus-write default this Job binds, and
# `scripts/post-deploy-verify.sh` Check 11 compares the live realm
# against the declared realm ConfigMap UNION this script's/the
# in-cluster Job's intent for exactly this reason. Do NOT narrow this
# array to "fix" a Check 11 false-positive read of the raw JSON file —
# the JSON was never meant to be the whole picture for admin-client.
# See `docs/architecture/sequence-oauth2-flow.md` (client → scope
# matrix) for the written rationale.
SCOPES=(
  "memory:episodic:write"
  "memory:procedural:write"
  "memory:semantic:write"
  "memory:decisions:write"
  "memory:skills:write"
  "audittrace:admin"
  "audittrace:assessment:ingest"
)

# ADR-062 §4 — Layer 5 (Shared Corpus) granular scopes. Kept in a
# separate array from SCOPES above because they are NOT bound with the
# same CLIENT_KIND fan-out below: corpus scopes are operator/curator-tier
# only (admin-client, optional) and must NEVER reach audittrace-opencode
# or audittrace-webui in either scope set (WU-A3,
# project_restricted_client_sc09_reserved). See the dedicated bind loop
# after the SCOPES one.
CORPUS_SCOPES=(
  "memory:corpus:decisions:read"
  "memory:corpus:decisions:write"
  "memory:corpus:skills:read"
  "memory:corpus:skills:write"
  "memory:corpus:semantic:read"
  "memory:corpus:semantic:write"
)

# M3-WU-D2-1 (2026-08-30) — the Souvenirs panel's memory-proxy write
# scopes. Bound ONLY as OPTIONAL to audittrace-librechat (never default —
# the browser's own login token must never carry write access; only the
# BFF's memory-path RFC 8693 exchange requests these by name). Mirrors
# the in-cluster Job's ConfigMap array verbatim
# (tests/test_chart_drift_guards.py::TestConsoleMemoryProxyScopeGovernance
# asserts the two stay in lock-step). A subset of SCOPES above, so Step 1
# ("Ensure each scope exists") already creates them; this is a separate
# array purely to isolate the bind loop from audittrace:admin/
# assessment:ingest, which SCOPES also carries.
MEMORY_CONSOLE_WRITE_SCOPES=(
  "memory:episodic:write"
  "memory:procedural:write"
  "memory:semantic:write"
)

# WU-1 (Sovereign-Attach EPIC, 2026-09-03) — the ephemeral chat-upload
# least-privilege wall. Bound ONLY as OPTIONAL to audittrace-librechat, in
# its OWN array/bind loop — kept separate from MEMORY_CONSOLE_WRITE_SCOPES
# above (the in-cluster Job's ConfigMap mirrors this verbatim; see
# tests/test_chart_drift_guards.py::TestKeycloakSessionWriteScopeGovernance
# for why: TestD25AMemoryWriteScopesJobRenderedBinding asserts
# MEMORY_CONSOLE_WRITE_SCOPES stays EXACTLY {episodic,procedural,semantic}
# :write, so folding memory:session:write into it would break that guard
# AND muddy the durable-vs-ephemeral distinction this WU exists to make
# real. Only WU-2's narrow upload exchange requests this scope by name.
MEMORY_SESSION_WRITE_SCOPES=(
  "memory:session:write"
)

# WU-5 (Sovereign-Attach EPIC, 2026-09-06) — same-turn recall of the
# caller's OWN ephemeral session uploads (recall_attachments). Bound as
# DEFAULT to audittrace-librechat (see the in-cluster Job's ConfigMap for
# the full rationale: this is a READ-OWN scope, same family as
# memory:conversational:read-own/memory:semantic:read, both already
# DEFAULT — unlike MEMORY_SESSION_WRITE_SCOPES above). Own array/bind
# loop, separate from the write array.
MEMORY_SESSION_READ_SCOPES=(
  "memory:session:read-own"
)

# Mongo-repl WU-1 (2026-09-11) — the console-conversations store's write
# scope. Bound ONLY as OPTIONAL to audittrace-librechat (never default),
# own array/bind loop — only the BFF's console-conversations proxy
# exchange requests it by name.
MEMORY_CONVERSATIONS_WRITE_SCOPES=(
  "memory:conversations:write"
)

# Mongo-repl WU-1 (2026-09-11) — the console-conversations store's
# read-own scope. Bound as DEFAULT to audittrace-librechat — same
# rationale as MEMORY_SESSION_READ_SCOPES above.
MEMORY_CONVERSATIONS_READ_SCOPES=(
  "memory:conversations:read-own"
)

# Mongo-repl WU-presets (2026-09-11) — the console-presets store's write
# scope. Bound ONLY as OPTIONAL to audittrace-librechat (never default),
# own array/bind loop — only the BFF's console-presets proxy exchange
# requests it by name.
MEMORY_PRESETS_WRITE_SCOPES=(
  "memory:presets:write"
)

# Mongo-repl WU-presets (2026-09-11) — the console-presets store's
# read-own scope. Bound as DEFAULT to audittrace-librechat — same
# rationale as MEMORY_CONVERSATIONS_READ_SCOPES above.
MEMORY_PRESETS_READ_SCOPES=(
  "memory:presets:read-own"
)

# Mongo-repl WU-prompts (2026-09-11) — the console-prompts store's
# write scope. Bound ONLY as OPTIONAL to audittrace-librechat (never
# default), own array/bind loop — only the BFF's console-prompts
# proxy exchange requests it by name.
MEMORY_PROMPTS_WRITE_SCOPES=(
  "memory:prompts:write"
)

# Mongo-repl WU-prompts (2026-09-11) — the console-prompts store's
# read-own scope. Bound as DEFAULT to audittrace-librechat — same
# rationale as MEMORY_PRESETS_READ_SCOPES above.
MEMORY_PROMPTS_READ_SCOPES=(
  "memory:prompts:read-own"
)

# Chat-Projects domain (2026-09-11, MongoDB-elimination EPIC) — the
# console-chat-projects store's write scope. Bound ONLY as OPTIONAL to
# audittrace-librechat (never default), own array/bind loop — only the
# BFF's console-chat-projects proxy exchange requests it by name.
MEMORY_CHAT_PROJECTS_WRITE_SCOPES=(
  "memory:chat_projects:write"
)

# Chat-Projects domain (2026-09-11) — the console-chat-projects store's
# read-own scope. Bound as DEFAULT to audittrace-librechat — same
# rationale as MEMORY_PROMPTS_READ_SCOPES above.
MEMORY_CHAT_PROJECTS_READ_SCOPES=(
  "memory:chat_projects:read-own"
)

# Files-metadata domain (2026-09-11, MongoDB-elimination EPIC) — the
# console-files store's write scope. Bound ONLY as OPTIONAL to
# audittrace-librechat (never default), own array/bind loop — only the
# BFF's console-files proxy exchange requests it by name.
MEMORY_FILES_WRITE_SCOPES=(
  "memory:files:write"
)

# Files-metadata domain (2026-09-11) — the console-files store's
# read-own scope. Bound as DEFAULT to audittrace-librechat — same
# rationale as MEMORY_CHAT_PROJECTS_READ_SCOPES above.
MEMORY_FILES_READ_SCOPES=(
  "memory:files:read-own"
)

# Agents domain (2026-09-12, MongoDB-elimination EPIC) — the
# console-agents store's write scope. Bound ONLY as OPTIONAL to
# audittrace-librechat (never default), own array/bind loop — only the
# BFF's console-agents proxy exchange requests it by name.
MEMORY_AGENTS_WRITE_SCOPES=(
  "memory:agents:write"
)

# Agents domain (2026-09-12) — the console-agents store's read-own
# scope. Bound as DEFAULT to audittrace-librechat — same rationale as
# MEMORY_FILES_READ_SCOPES above.
MEMORY_AGENTS_READ_SCOPES=(
  "memory:agents:read-own"
)

# Conversation-Tags domain (2026-09-13, MongoDB-elimination EPIC) — the
# console-conversation-tags store's write scope. Bound ONLY as OPTIONAL
# to audittrace-librechat (never default), own array/bind loop — only
# the BFF's console-conversation-tags proxy exchange requests it by
# name.
MEMORY_CONVERSATION_TAGS_WRITE_SCOPES=(
  "memory:conversation_tags:write"
)

# Conversation-Tags domain (2026-09-13) — the console-conversation-tags
# store's read-own scope. Bound as DEFAULT to audittrace-librechat —
# same rationale as MEMORY_AGENTS_READ_SCOPES above.
MEMORY_CONVERSATION_TAGS_READ_SCOPES=(
  "memory:conversation_tags:read-own"
)

# Tool-Favorites domain (2026-09-13, MongoDB-elimination EPIC) — the
# console-tool-favorites store's write scope. Bound ONLY as OPTIONAL to
# audittrace-librechat (never default), own array/bind loop — only the
# BFF's console-tool-favorites proxy exchange requests it by name.
MEMORY_TOOL_FAVORITES_WRITE_SCOPES=(
  "memory:tool_favorites:write"
)

# Tool-Favorites domain (2026-09-13) — the console-tool-favorites
# store's read-own scope. Bound as DEFAULT to audittrace-librechat —
# same rationale as MEMORY_CONVERSATION_TAGS_READ_SCOPES above.
MEMORY_TOOL_FAVORITES_READ_SCOPES=(
  "memory:tool_favorites:read-own"
)

# Sovereign Authorization Layer EPIC, WU-1 (2026-09-17, READ PATH ONLY)
# — the console-ACL store's read-own scope. Bound as DEFAULT to
# audittrace-librechat — same rationale as
# MEMORY_TOOL_FAVORITES_READ_SCOPES above.
MEMORY_ACL_READ_SCOPES=(
  "memory:acl:read-own"
)

# Sovereign Authorization Layer EPIC, WU-2c — the console-ACL WRITE
# scope. Bound ONLY as OPTIONAL to audittrace-librechat (like every
# console write scope) — own array/bind loop; the dedicated E2E client
# gets it through its own step below, never through this loop.
MEMORY_ACL_WRITE_SCOPES=(
  "memory:acl:write"
)

# ----- Ensure each scope exists -----
declare -A SCOPE_ID
ACL_E2E_STAGE="scope-ensure"
for SCOPE in "${SCOPES[@]}" "${CORPUS_SCOPES[@]}" "${MEMORY_SESSION_WRITE_SCOPES[@]}" "${MEMORY_SESSION_READ_SCOPES[@]}" "${MEMORY_CONVERSATIONS_WRITE_SCOPES[@]}" "${MEMORY_CONVERSATIONS_READ_SCOPES[@]}" "${MEMORY_PRESETS_WRITE_SCOPES[@]}" "${MEMORY_PRESETS_READ_SCOPES[@]}" "${MEMORY_PROMPTS_WRITE_SCOPES[@]}" "${MEMORY_PROMPTS_READ_SCOPES[@]}" "${MEMORY_CHAT_PROJECTS_WRITE_SCOPES[@]}" "${MEMORY_CHAT_PROJECTS_READ_SCOPES[@]}" "${MEMORY_FILES_WRITE_SCOPES[@]}" "${MEMORY_FILES_READ_SCOPES[@]}" "${MEMORY_AGENTS_WRITE_SCOPES[@]}" "${MEMORY_AGENTS_READ_SCOPES[@]}" "${MEMORY_CONVERSATION_TAGS_WRITE_SCOPES[@]}" "${MEMORY_CONVERSATION_TAGS_READ_SCOPES[@]}" "${MEMORY_TOOL_FAVORITES_WRITE_SCOPES[@]}" "${MEMORY_TOOL_FAVORITES_READ_SCOPES[@]}" "${MEMORY_ACL_READ_SCOPES[@]}" "${MEMORY_ACL_WRITE_SCOPES[@]}"; do
  EXISTING=$(acl_e2e_kc get client-scopes -r "${REALM}" \
               --fields id,name --format csv --noquotes 2>/dev/null \
             | awk -F, -v n="${SCOPE}" '$2 == n {print $1; exit}')
  if [[ -n "${EXISTING}" ]]; then
    echo "  ⊝ scope ${SCOPE}: exists (${EXISTING})"
    SCOPE_ID["${SCOPE}"]="${EXISTING}"
  else
    acl_e2e_kc create client-scopes -r "${REALM}" \
      -s "name=${SCOPE}" \
      -s protocol=openid-connect \
      -s 'attributes."include.in.token.scope"=true' >/dev/null
    NEW_ID=$(acl_e2e_kc get client-scopes -r "${REALM}" \
               --fields id,name --format csv --noquotes \
             | awk -F, -v n="${SCOPE}" '$2 == n {print $1; exit}')
    echo "  ✓ scope ${SCOPE}: created (${NEW_ID})"
    SCOPE_ID["${SCOPE}"]="${NEW_ID}"
  fi
done

# ----- Bind scopes to clients -----
declare -A CLIENT_KIND
CLIENT_KIND["admin-client"]="default"
CLIENT_KIND["audittrace-opencode"]="optional"
CLIENT_KIND["audittrace-webui"]="optional"

bind_scope() {
  local CLIENT_ID="$1" SCOPE="$2" KIND="$3"
  local CLIENT_UUID SCOPE_UUID PATH_SUFFIX
  ACL_E2E_STAGE="bind"

  CLIENT_UUID=$(acl_e2e_kc get clients -r "${REALM}" \
                  -q "clientId=${CLIENT_ID}" \
                  --fields id --format csv --noquotes 2>/dev/null \
                | tr -d '"' | head -1)
  if [[ -z "${CLIENT_UUID}" ]]; then
    echo "  ⚠ client ${CLIENT_ID}: not found (skipped)"
    return 0
  fi
  SCOPE_UUID="${SCOPE_ID[$SCOPE]:-}"
  if [[ -z "${SCOPE_UUID}" ]]; then
    echo "❌ scope ${SCOPE}: id not resolvable — bug" >&2
    return 1
  fi

  if [[ "${KIND}" == "default" ]]; then
    PATH_SUFFIX="default-client-scopes"
  else
    PATH_SUFFIX="optional-client-scopes"
  fi

  # `-b '{}'` not `-s ''` — see configmap-memory-scopes-script.yaml
  # for the 2026-05-03 lesson on why the kcadm binding command needs
  # an explicit empty JSON body rather than an empty -s argument.
  if ! acl_e2e_kc update \
         "clients/${CLIENT_UUID}/${PATH_SUFFIX}/${SCOPE_UUID}" \
         -r "${REALM}" -b '{}' >/dev/null 2>&1; then
    echo "  ✗ ${CLIENT_ID} ${KIND} ← ${SCOPE} (kcadm rejected the bind)" >&2
    return 1
  fi
  echo "  ✓ ${CLIENT_ID} ${KIND} ← ${SCOPE}"
}

for CLIENT_ID in "${!CLIENT_KIND[@]}"; do
  KIND="${CLIENT_KIND[$CLIENT_ID]}"
  echo "▶ binding scopes to client ${CLIENT_ID} (${KIND})..."
  for SCOPE in "${SCOPES[@]}"; do
    bind_scope "${CLIENT_ID}" "${SCOPE}" "${KIND}"
  done
done

# ----- Bind corpus scopes (operator/curator-tier only) -----
# admin-client only, as OPTIONAL — never audittrace-opencode or
# audittrace-webui, and never audittrace-restricted (SC-09). This is a
# separate loop (not folded into CLIENT_KIND above) precisely so a
# future edit to CLIENT_KIND cannot silently widen corpus-scope
# distribution to the user-facing clients.
echo "▶ binding corpus scopes to client admin-client (optional)..."
for SCOPE in "${CORPUS_SCOPES[@]}"; do
  bind_scope "admin-client" "${SCOPE}" "optional"
done

# ----- Bind Souvenirs-panel memory-write scopes (M3-WU-D2-1) -----
# audittrace-librechat only, as OPTIONAL. Never audittrace:admin.
echo "▶ binding memory-proxy write scopes to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_CONSOLE_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- Bind the ephemeral-ingest scope (WU-1, 2026-09-03) -----
# audittrace-librechat only, as OPTIONAL — separate loop from the one
# above (see MEMORY_SESSION_WRITE_SCOPES's comment).
echo "▶ binding ephemeral-session write scope to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_SESSION_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- Bind the ephemeral-session READ-OWN scope (WU-5, 2026-09-06) -----
# audittrace-librechat only, as DEFAULT (see MEMORY_SESSION_READ_SCOPES's
# comment above).
echo "▶ binding ephemeral-session read-own scope to client audittrace-librechat (default)..."
for SCOPE in "${MEMORY_SESSION_READ_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "default"
done

# ----- Bind the console-conversations WRITE scope (Mongo-repl WU-1) -----
# audittrace-librechat only, as OPTIONAL — separate loop from the one
# above.
echo "▶ binding console-conversations write scope to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_CONVERSATIONS_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- Bind the console-conversations READ-OWN scope (Mongo-repl WU-1) -----
# audittrace-librechat only, as DEFAULT — same rationale as the
# ephemeral-session read-own bind above.
echo "▶ binding console-conversations read-own scope to client audittrace-librechat (default)..."
for SCOPE in "${MEMORY_CONVERSATIONS_READ_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "default"
done

# ----- Bind the console-presets WRITE scope (Mongo-repl WU-presets) -----
# audittrace-librechat only, as OPTIONAL — separate loop from the ones
# above.
echo "▶ binding console-presets write scope to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_PRESETS_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- Bind the console-presets READ-OWN scope (Mongo-repl WU-presets) -----
# audittrace-librechat only, as DEFAULT — same rationale as the
# console-conversations read-own bind above.
echo "▶ binding console-presets read-own scope to client audittrace-librechat (default)..."
for SCOPE in "${MEMORY_PRESETS_READ_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "default"
done

# ----- Bind the console-prompts WRITE scope (Mongo-repl WU-prompts) -----
# audittrace-librechat only, as OPTIONAL — separate loop from the ones
# above.
echo "▶ binding console-prompts write scope to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_PROMPTS_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- Bind the console-prompts READ-OWN scope (Mongo-repl WU-prompts) -----
# audittrace-librechat only, as DEFAULT — same rationale as the
# console-presets read-own bind above.
echo "▶ binding console-prompts read-own scope to client audittrace-librechat (default)..."
for SCOPE in "${MEMORY_PROMPTS_READ_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "default"
done

# ----- Bind the console-chat-projects WRITE scope (Chat-Projects domain) -----
# audittrace-librechat only, as OPTIONAL — separate loop from the ones
# above.
echo "▶ binding console-chat-projects write scope to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_CHAT_PROJECTS_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- Bind the console-chat-projects READ-OWN scope (Chat-Projects domain) -----
# audittrace-librechat only, as DEFAULT — same rationale as the
# console-prompts read-own bind above.
echo "▶ binding console-chat-projects read-own scope to client audittrace-librechat (default)..."
for SCOPE in "${MEMORY_CHAT_PROJECTS_READ_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "default"
done

# ----- Bind the console-files WRITE scope (Files-metadata domain) -----
# audittrace-librechat only, as OPTIONAL — separate loop from the ones
# above.
echo "▶ binding console-files write scope to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_FILES_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- Bind the console-files READ-OWN scope (Files-metadata domain) -----
# audittrace-librechat only, as DEFAULT — same rationale as the
# console-chat-projects read-own bind above.
echo "▶ binding console-files read-own scope to client audittrace-librechat (default)..."
for SCOPE in "${MEMORY_FILES_READ_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "default"
done

# ----- Bind the console-agents WRITE scope (Agents domain) -----
# audittrace-librechat only, as OPTIONAL — separate loop from the ones
# above.
echo "▶ binding console-agents write scope to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_AGENTS_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- Bind the console-agents READ-OWN scope (Agents domain) -----
# audittrace-librechat only, as DEFAULT — same rationale as the
# console-files read-own bind above.
echo "▶ binding console-agents read-own scope to client audittrace-librechat (default)..."
for SCOPE in "${MEMORY_AGENTS_READ_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "default"
done

# ----- Bind the console-conversation-tags WRITE scope (Conversation-Tags domain) -----
# audittrace-librechat only, as OPTIONAL — separate loop from the ones
# above.
echo "▶ binding console-conversation-tags write scope to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_CONVERSATION_TAGS_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- Bind the console-conversation-tags READ-OWN scope (Conversation-Tags domain) -----
# audittrace-librechat only, as DEFAULT — same rationale as the
# console-agents read-own bind above.
echo "▶ binding console-conversation-tags read-own scope to client audittrace-librechat (default)..."
for SCOPE in "${MEMORY_CONVERSATION_TAGS_READ_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "default"
done

# ----- Bind the console-tool-favorites WRITE scope (Tool-Favorites domain) -----
# audittrace-librechat only, as OPTIONAL — separate loop from the ones
# above.
echo "▶ binding console-tool-favorites write scope to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_TOOL_FAVORITES_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- Bind the console-tool-favorites READ-OWN scope (Tool-Favorites domain) -----
# audittrace-librechat only, as DEFAULT — same rationale as the
# console-conversation-tags read-own bind above.
echo "▶ binding console-tool-favorites read-own scope to client audittrace-librechat (default)..."
for SCOPE in "${MEMORY_TOOL_FAVORITES_READ_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "default"
done

# ----- Bind the console-ACL READ-OWN scope (Sovereign Authorization Layer EPIC, WU-1) -----
# audittrace-librechat only, as DEFAULT — same rationale as the
# console-tool-favorites read-own bind above.
echo "▶ binding console-acl read-own scope to client audittrace-librechat (default)..."
for SCOPE in "${MEMORY_ACL_READ_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "default"
done

# ----- Bind the console-ACL WRITE scope (WU-2c) -----
# audittrace-librechat only, as OPTIONAL — like every console write scope.
echo "▶ binding console-acl write scope to client audittrace-librechat (optional)..."
for SCOPE in "${MEMORY_ACL_WRITE_SCOPES[@]}"; do
  bind_scope "audittrace-librechat" "${SCOPE}" "optional"
done

# ----- ACL WU-2c: the dedicated E2E-only device-flow client (audittrace-acl-e2e) -----
# Flag-aware (AUDITTRACE_ACL_E2E_CLIENT_ENABLED, set by the hook Job from
# .Values.keycloak.aclE2eClient.enabled; default true): enabled ->
# ensure + VERIFY (fail closed on drift, never `kcadm update` a drifted
# client); disabled -> verify-then-delete (exactly one match, clientId
# equal) and read back an empty result. This step sits OUTSIDE the
# MEMORY_ACL_*_SCOPES bind loops. The pinned JSON below is byte-identical
# in scripts/setup-memory-scopes.sh and the chart ConfigMap (parity-guarded).
ACL_E2E_CLIENT_ID="audittrace-acl-e2e"
ACL_E2E_ENABLED="${AUDITTRACE_ACL_E2E_CLIENT_ENABLED:-true}"
ACL_E2E_CLIENT_JSON='{"clientId":"audittrace-acl-e2e","description":"ACL WU-2c E2E-only device-flow client (sunset: WU-4 merge, keycloak.aclE2eClient.enabled=false). Holds ONLY memory:acl:read-own + memory:acl:write; no offline_access, no refresh, consent required.","enabled":true,"protocol":"openid-connect","publicClient":true,"standardFlowEnabled":false,"directAccessGrantsEnabled":false,"implicitFlowEnabled":false,"serviceAccountsEnabled":false,"consentRequired":true,"redirectUris":["urn:ietf:wg:oauth:2.0:oob"],"webOrigins":[],"attributes":{"oauth2.device.authorization.grant.enabled":"true","oauth2.device.polling.interval":"5","oauth2.device.code.lifespan":"120","use.refresh.tokens":"false","access.token.lifespan":"900"},"defaultClientScopes":["memory:acl:read-own","memory:acl:write"],"optionalClientScopes":[],"protocolMappers":[{"name":"aud-audittrace-server","protocol":"openid-connect","protocolMapper":"oidc-audience-mapper","config":{"included.custom.audience":"audittrace-server","id.token.claim":"false","access.token.claim":"true"}}]}'
ACL_E2E_DEFAULT_SCOPES=("memory:acl:read-own" "memory:acl:write")
ACL_E2E_STAGE="pre-step:init"

# Whitespace-stripped JSON of one kcadm GET (so `"k" : "v"` -> `"k":"v"`).
acl_e2e_compact() {
  local raw
  raw=$(acl_e2e_kc get "$1" -r "${REALM}") || acl_e2e_fail read-failed "cannot read the client"
  echo "${raw//[[:space:]]/}"
}

# Names of a client's default|optional scopes, one per line.
acl_e2e_scope_names() {
  local out line
  out=$(acl_e2e_kc get "clients/$1/$2-client-scopes" -r "${REALM}" \
          --fields name --format csv --noquotes) \
    || acl_e2e_fail read-failed "cannot read $2 scopes"
  while IFS= read -r line; do
    line="${line//\"/}"
    [[ -n "${line}" ]] && echo "${line}"
  done <<< "${out}"
  return 0
}

acl_e2e_in_default_set() {
  local want
  for want in "${ACL_E2E_DEFAULT_SCOPES[@]}"; do
    [[ "$1" == "${want}" ]] && return 0
  done
  return 1
}

# Remove every default/optional scope NOT in the pinned set (Keycloak may
# add realm-default scopes on create): default keeps the two ACL scopes,
# optional keeps none.
acl_e2e_strip_scopes() {
  local uuid="$1" kind out sid sname keep
  ACL_E2E_STAGE="strip"
  for kind in default optional; do
    out=$(acl_e2e_kc get "clients/${uuid}/${kind}-client-scopes" -r "${REALM}" \
            --fields id,name --format csv --noquotes) \
      || acl_e2e_fail strip-list-failed "cannot list ${kind} scopes"
    while IFS=, read -r sid sname; do
      sid="${sid//\"/}"; sname="${sname//\"/}"
      [[ -z "${sid}" ]] && continue
      keep=0
      if [[ "${kind}" == "default" ]] && acl_e2e_in_default_set "${sname}"; then
        keep=1
      fi
      if [[ "${keep}" -eq 0 ]]; then
        acl_e2e_kc delete "clients/${uuid}/${kind}-client-scopes/${sid}" \
          -r "${REALM}" >/dev/null \
          || acl_e2e_fail strip-delete-failed "could not strip unpinned ${kind} scope"
        echo "  ✓ ${ACL_E2E_CLIENT_ID}: stripped unpinned ${kind} scope ${sname}"
      fi
    done <<< "${out}"
  done
}

# VERIFY (equality on the compared keys). Flags + consentRequired exact;
# attributes a pinned SUBSET; default/optional scopes EXACT sets; protocol
# mappers EXACT (name set == {aud-audittrace-server}, protocolMapper and
# config pinned). Any difference -> exit non-zero with the diff.
acl_e2e_verify() {
  local uuid="$1" diffs="" compact item name names raw line incfg cfgblocks pmok n=0
  local unexpected_default=0 unexpected_optional=0 unexpected_mapper=0 unexpected_config=0
  ACL_E2E_STAGE="verify-client"
  compact=$(acl_e2e_compact "clients/${uuid}") \
    || acl_e2e_fail read-failed "cannot read the client"
  for item in \
      '"clientId":"audittrace-acl-e2e"' '"publicClient":true' \
      '"standardFlowEnabled":false' '"directAccessGrantsEnabled":false' \
      '"implicitFlowEnabled":false' '"serviceAccountsEnabled":false' \
      '"consentRequired":true' \
      '"oauth2.device.authorization.grant.enabled":"true"' \
      '"oauth2.device.polling.interval":"5"' \
      '"oauth2.device.code.lifespan":"120"' \
      '"use.refresh.tokens":"false"' '"access.token.lifespan":"900"'; do
    [[ "${compact}" == *"${item}"* ]] || diffs+=" missing:${item}"
  done
  # default scopes: exact set (read FIRST, so a failed read is fatal and
  # can never look like an empty/equal set)
  ACL_E2E_STAGE="verify-default-scopes"
  names=$(acl_e2e_scope_names "${uuid}" default) \
    || acl_e2e_fail read-failed "cannot read default scopes"
  n=0
  unexpected_default=0
  while IFS= read -r name; do
    [[ -z "${name}" ]] && continue
    n=$((n + 1))
    acl_e2e_in_default_set "${name}" || unexpected_default=$((unexpected_default + 1))
  done <<< "${names}"
  # F4 §5 / R4: detail= is a CLOSED VOCABULARY built from PROPERTIES
  # (counts, shapes) — NEVER a byte of the response itself. A deny-list
  # mask on specific secret SHAPES (the prior round's fix) is beatable by
  # any shape it doesn't name; omitting response-derived text entirely is
  # the only fail-closed form.
  [[ "${unexpected_default}" -eq 0 ]] \
    || diffs+=" unexpected-default-scope:count=${unexpected_default}"
  [[ "${n}" -eq "${#ACL_E2E_DEFAULT_SCOPES[@]}" ]] \
    || diffs+=" default-scope-count:${n}"
  # optional scopes: exact empty set
  ACL_E2E_STAGE="verify-optional-scopes"
  names=$(acl_e2e_scope_names "${uuid}" optional) \
    || acl_e2e_fail read-failed "cannot read optional scopes"
  unexpected_optional=0
  while IFS= read -r name; do
    [[ -z "${name}" ]] && continue
    unexpected_optional=$((unexpected_optional + 1))
  done <<< "${names}"
  [[ "${unexpected_optional}" -eq 0 ]] \
    || diffs+=" unexpected-optional-scope:count=${unexpected_optional}"
  # protocol mappers: exact name set + pinned protocolMapper/config
  ACL_E2E_STAGE="verify-mappers"
  names=$(acl_e2e_kc get "clients/${uuid}/protocol-mappers/models" -r "${REALM}" \
            --fields name --format csv --noquotes) \
    || acl_e2e_fail read-failed "cannot read protocol mappers"
  n=0
  unexpected_mapper=0
  while IFS= read -r name; do
    name="${name//\"/}"
    [[ -z "${name}" ]] && continue
    n=$((n + 1))
    [[ "${name}" == "aud-audittrace-server" ]] || unexpected_mapper=$((unexpected_mapper + 1))
  done <<< "${names}"
  [[ "${unexpected_mapper}" -eq 0 ]] \
    || diffs+=" unexpected-mapper:count=${unexpected_mapper}"
  [[ "${n}" -eq 1 ]] || diffs+=" mapper-count:${n}"
  ACL_E2E_STAGE="verify-mapper-config"
  raw=$(acl_e2e_kc get "clients/${uuid}/protocol-mappers/models" -r "${REALM}") \
    || acl_e2e_fail read-failed "cannot read the mapper config"
  # SC-1, EXACT: the mapper's name and protocolMapper equal the pinned
  # values and its `config` map holds EXACTLY the pinned three pairs - no
  # extra key (an audience widening or a hardcoded-claim `scope` writer
  # would be one), none missing. Lines are compared VERBATIM: only the
  # indentation, a trailing comma and the `" : "` key separator of kcadm's
  # pretty-printed JSON are normalised, so whitespace INSIDE a value (for
  # example `audittrace -server`) is a difference. This relies on kcadm's
  # one-key-per-line pretty output of a ProtocolMapperRepresentation (id,
  # name, protocol, protocolMapper, consentRequired, config - `config` is
  # a flat string map with unique keys); a compact or re-shaped output
  # finds no `config` block and fails closed (mapper-config-absent). No
  # allow-list: if a real Keycloak adds a config key on create, this fails
  # closed and the spec is amended.
  incfg=0
  cfgblocks=0
  pmok=0
  n=0
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    line="${line%,}"
    line="${line/ : /:}"
    if [[ "${incfg}" -eq 1 ]]; then
      if [[ "${line}" == "}" ]]; then
        incfg=0
        continue
      fi
      n=$((n + 1))
      case "${line}" in
        '"included.custom.audience":"audittrace-server"' | '"id.token.claim":"false"' | '"access.token.claim":"true"') ;;
        *) unexpected_config=$((unexpected_config + 1)) ;;
      esac
    elif [[ "${line}" == '"config":{' ]]; then
      incfg=1
      cfgblocks=$((cfgblocks + 1))
    elif [[ "${line}" == '"protocolMapper":"oidc-audience-mapper"' ]]; then
      pmok=1
    fi
  done <<< "${raw}"
  [[ "${unexpected_config}" -eq 0 ]] \
    || diffs+=" unexpected-mapper-config:count=${unexpected_config}"
  [[ "${pmok}" -eq 1 ]] || diffs+=" missing-mapper-field:protocolMapper"
  if [[ "${cfgblocks}" -ne 1 ]]; then
    diffs+=" mapper-config-absent"
  else
    [[ "${n}" -eq 3 ]] || diffs+=" mapper-config-count:${n}"
  fi
  if [[ -n "${diffs}" ]]; then
    acl_e2e_fail drift "drift from the pinned client (fail closed, no kcadm update):${diffs}"
  fi
  echo "  ✓ ${ACL_E2E_CLIENT_ID}: verified equal to the pinned block"
}

# Matching clients as csv `id,clientId` lines; query failure is fatal.
# Read-only shape capture (F §3 / F1 §5 / F2 §5 / F3 S2-6): default off
# (AUDITTRACE_ACL_E2E_SHAPE_CAPTURE); prints key names with VALUES typed
# and redacted — never a value — between `acl-e2e: shape-begin <what>` /
# `acl-e2e: shape-end` markers; issues GETs only, changes no control
# flow. Pure bash (the image has no jq/awk/curl). Any line not matching
# an allowlisted shape is `<line:redacted len=n>` (fail closed) — a
# public client's representation may carry `registrationAccessToken`,
# and the rule must not depend on where it sits.
acl_e2e_shape_line() {
  local line="$1" trimmed key rest trail=""
  trimmed="${line#"${line%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  case "${trimmed}" in
    '{' | '[' | '},' | '],' | '}' | ']')
      printf '%s\n' "${trimmed}"
      return
      ;;
  esac
  if [[ "${trimmed}" == *, ]]; then
    trail=","
    trimmed="${trimmed%,}"
  fi
  if [[ "${trimmed}" =~ ^\"([^\"]*)\"\ :\ (.*)$ ]]; then
    key="\"${BASH_REMATCH[1]}\""
    rest="${BASH_REMATCH[2]}"
    if [[ "${rest}" =~ ^\"(.*)\"$ ]]; then
      printf '%s: <string:%d>%s\n' "${key}" "${#rest}" "${trail}"
    elif [[ "${rest}" == "true" || "${rest}" == "false" ]]; then
      printf '%s: <bool>%s\n' "${key}" "${trail}"
    elif [[ "${rest}" == "null" ]]; then
      printf '%s: <null>%s\n' "${key}" "${trail}"
    elif [[ "${rest}" == "{" || "${rest}" == "{}" ]]; then
      printf '%s: %s%s\n' "${key}" "${rest}" "${trail}"
    elif [[ "${rest}" == "[" || "${rest}" == "[]" ]]; then
      printf '%s: %s%s\n' "${key}" "${rest}" "${trail}"
    elif [[ "${rest}" =~ ^\[(.*)\]$ ]]; then
      local inner="${BASH_REMATCH[1]}" count=0 i
      if [[ -n "${inner//[[:space:]]/}" ]]; then
        count=1
        for ((i = 0; i < ${#inner}; i++)); do
          [[ "${inner:i:1}" == "," ]] && count=$((count + 1))
        done
      fi
      printf '%s: <array:%d>%s\n' "${key}" "${count}" "${trail}"
    elif [[ "${rest}" =~ ^[0-9]+$ ]]; then
      printf '%s: <number>%s\n' "${key}" "${trail}"
    else
      printf '<line:redacted len=%d>\n' "${#line}"
    fi
    return
  fi
  if [[ "${trimmed}" =~ ^\"(.*)\"$ ]]; then
    printf '<string:%d>%s\n' "${#trimmed}" "${trail}"
    return
  fi
  printf '<line:redacted len=%d>\n' "${#line}"
}

acl_e2e_shape_capture() {
  local what="$1" uuid="$2" raw line
  [[ "${AUDITTRACE_ACL_E2E_SHAPE_CAPTURE:-0}" == "1" ]] || return 0
  echo "acl-e2e: shape-begin ${what}"
  for path in "clients/${uuid}" "clients/${uuid}/default-client-scopes" \
              "clients/${uuid}/optional-client-scopes" \
              "clients/${uuid}/protocol-mappers/models"; do
    raw=$(acl_e2e_kc get "${path}" -r "${REALM}") || continue
    while IFS= read -r line; do
      acl_e2e_shape_line "${line}"
    done <<< "${raw}"
  done
  echo "acl-e2e: shape-end"
}

acl_e2e_lookup() {
  local out
  out=$(acl_e2e_kc get clients -r "${REALM}" -q "clientId=${ACL_E2E_CLIENT_ID}" \
          --fields id,clientId --format csv --noquotes) \
    || acl_e2e_fail lookup-failed "client lookup failed"
  echo "${out//\"/}"
}

if [[ "${ACL_E2E_ENABLED}" == "true" ]]; then
  echo "▶ ensuring the dedicated E2E client ${ACL_E2E_CLIENT_ID} (enabled)..."
  ACL_E2E_STAGE="lookup"
  ACL_E2E_FOUND=$(acl_e2e_lookup) || acl_e2e_fail lookup-failed "client lookup failed"
  if [[ -z "${ACL_E2E_FOUND//[[:space:]]/}" ]]; then
    ACL_E2E_STAGE="create"
    printf '%s' "${ACL_E2E_CLIENT_JSON}" | acl_e2e_kc create clients -r "${REALM}" -f - >/dev/null \
      || acl_e2e_fail create-failed "create failed"
    echo "  ✓ ${ACL_E2E_CLIENT_ID}: created"
    ACL_E2E_STAGE="lookup"
    ACL_E2E_UUID=$(acl_e2e_lookup) || acl_e2e_fail lookup-failed "client lookup failed"
    ACL_E2E_UUID="${ACL_E2E_UUID%%,*}"
    [[ -n "${ACL_E2E_UUID}" ]] || acl_e2e_fail id-unresolvable "created but id not resolvable"
    acl_e2e_strip_scopes "${ACL_E2E_UUID}"
    ACL_E2E_STAGE="bind"
    for SCOPE in "${ACL_E2E_DEFAULT_SCOPES[@]}"; do
      bind_scope "${ACL_E2E_CLIENT_ID}" "${SCOPE}" "default"
    done
    acl_e2e_shape_capture after-create "${ACL_E2E_UUID}"
  else
    [[ "$(echo "${ACL_E2E_FOUND}" | grep -c .)" -eq 1 ]] \
      || acl_e2e_fail ambiguous-match "more than one client matches (refusing)"
    ACL_E2E_UUID="${ACL_E2E_FOUND%%,*}"
    acl_e2e_shape_capture after-lookup "${ACL_E2E_UUID}"
  fi
  acl_e2e_verify "${ACL_E2E_UUID}"
else
  echo "▶ removing the dedicated E2E client ${ACL_E2E_CLIENT_ID} (disabled — sunset)..."
  ACL_E2E_STAGE="sunset-lookup"
  ACL_E2E_FOUND=$(acl_e2e_lookup) || acl_e2e_fail lookup-failed "client lookup failed"
  if [[ -z "${ACL_E2E_FOUND//[[:space:]]/}" ]]; then
    echo "  ⊝ ${ACL_E2E_CLIENT_ID}: absent (empty query result)"
  else
    [[ "$(echo "${ACL_E2E_FOUND}" | grep -c .)" -eq 1 ]] \
      || acl_e2e_fail ambiguous-match "refusing to delete: query did not return exactly one client"
    [[ "${ACL_E2E_FOUND#*,}" == "${ACL_E2E_CLIENT_ID}" ]] \
      || acl_e2e_fail sunset-wrong-client-id "refusing to delete: returned clientId is not ${ACL_E2E_CLIENT_ID}"
    acl_e2e_shape_capture after-lookup "${ACL_E2E_FOUND%%,*}"
    ACL_E2E_STAGE="sunset-delete"
    acl_e2e_kc delete "clients/${ACL_E2E_FOUND%%,*}" -r "${REALM}" >/dev/null \
      || acl_e2e_fail sunset-delete-failed "delete failed"
    ACL_E2E_STAGE="sunset-readback"
    ACL_E2E_FOUND=$(acl_e2e_lookup) || acl_e2e_fail lookup-failed "read-back lookup failed"
    [[ -z "${ACL_E2E_FOUND//[[:space:]]/}" ]] \
      || acl_e2e_fail sunset-readback-nonempty "read-back after delete is not empty"
    echo "  ✓ ${ACL_E2E_CLIENT_ID}: deleted (read-back empty)"
  fi
fi

# ----- User-identity protocol mappers -----
# Without these, JWTs from user-facing clients lack `preferred_username`,
# `email`, `name` etc. — only a bare UUID `sub`. Found in PR A's
# 2026-05-03 live test. See the in-cluster Job script for the full
# rationale on why direct mappers (not the standard `profile` scope).
# admin-client doesn't need this — service accounts have no human
# identity to surface.
ensure_mapper() {
  local CLIENT_ID="$1" MAPPER_NAME="$2" USER_ATTR="$3" CLAIM_NAME="$4"
  local CLIENT_UUID
  ACL_E2E_STAGE="mapper-ensure"
  CLIENT_UUID=$(acl_e2e_kc get clients -r "${REALM}" \
                  -q "clientId=${CLIENT_ID}" \
                  --fields id --format csv --noquotes 2>/dev/null \
                | tr -d '"' | head -1)
  if [[ -z "${CLIENT_UUID}" ]]; then
    echo "  ⚠ client ${CLIENT_ID}: not found (skipped)"
    return 0
  fi
  local existing
  existing=$(acl_e2e_kc get \
               "clients/${CLIENT_UUID}/protocol-mappers/models" \
               -r "${REALM}" --fields name --format csv --noquotes \
               2>/dev/null | grep -Fx "${MAPPER_NAME}" || true)
  if [[ -n "${existing}" ]]; then
    echo "  ⊝ ${CLIENT_ID} mapper ${MAPPER_NAME}: exists"
    return 0
  fi
  # Single-line JSON via printf — same shape as the in-cluster Job
  # script; see configmap-memory-scopes-script.yaml for the heredoc-vs-
  # YAML rationale.
  local body
  body=$(printf '{"name":"%s","protocol":"openid-connect","protocolMapper":"oidc-usermodel-property-mapper","config":{"user.attribute":"%s","claim.name":"%s","jsonType.label":"String","id.token.claim":"true","access.token.claim":"true","userinfo.token.claim":"true"}}' \
    "${MAPPER_NAME}" "${USER_ATTR}" "${CLAIM_NAME}")
  printf '%s' "${body}" | acl_e2e_kc create \
    "clients/${CLIENT_UUID}/protocol-mappers/models" \
    -r "${REALM}" -f - >/dev/null
  echo "  ✓ ${CLIENT_ID} mapper ${MAPPER_NAME}: created"
}

for CLIENT_ID in audittrace-opencode audittrace-webui; do
  echo "▶ ensuring user-identity mappers on ${CLIENT_ID}..."
  ensure_mapper "${CLIENT_ID}" preferred-username username preferred_username
  ensure_mapper "${CLIENT_ID}" email email email
  ensure_mapper "${CLIENT_ID}" given-name firstName given_name
  ensure_mapper "${CLIENT_ID}" family-name lastName family_name
done

# ----- RFC 8693 token-exchange permission (M3-WU-2b) -----
# Mirrors Step 4 of the in-cluster ensure-memory-scopes Job
# (configmap-memory-scopes-script.yaml) VERBATIM in client-name/policy
# choice — this backstop script and the in-cluster Job must authorize
# the exact same (source, target) pair or a disaster-recovery re-run
# from this script would silently diverge from what the Job would have
# provisioned. See that ConfigMap for the full kcadm-mechanism
# rationale + the live-verification note.
TOKEN_EXCHANGE_TARGET_CLIENT="audittrace-librechat"
TOKEN_EXCHANGE_SOURCE_CLIENT="audittrace-librechat-bff"
TOKEN_EXCHANGE_POLICY_NAME="allow-${TOKEN_EXCHANGE_SOURCE_CLIENT}-token-exchange"

find_client_id() {
  acl_e2e_kc get clients -r "${REALM}" \
    -q "clientId=$1" \
    --fields id --format csv --noquotes 2>/dev/null \
  | tr -d '"' | head -1
}

find_policy_id() {
  local target="$1" client_uuid="$2"
  acl_e2e_kc get "clients/${client_uuid}/authz/resource-server/policy" \
    -r "${REALM}" --fields id,name --format csv --noquotes 2>/dev/null \
  | awk -F, -v n="${target}" '$2 == n {print $1; exit}'
}

echo "▶ authorizing ${TOKEN_EXCHANGE_SOURCE_CLIENT} to exchange for ${TOKEN_EXCHANGE_TARGET_CLIENT}..."
ACL_E2E_STAGE="token-exchange"
TARGET_UUID=$(find_client_id "${TOKEN_EXCHANGE_TARGET_CLIENT}")
SOURCE_UUID=$(find_client_id "${TOKEN_EXCHANGE_SOURCE_CLIENT}")
REALM_MGMT_UUID=$(find_client_id "realm-management")
if [[ -z "${TARGET_UUID}" || -z "${SOURCE_UUID}" || -z "${REALM_MGMT_UUID}" ]]; then
  echo "❌ one of ${TOKEN_EXCHANGE_TARGET_CLIENT} / ${TOKEN_EXCHANGE_SOURCE_CLIENT} / realm-management not found — cannot authorize exchange." >&2
  exit 1
fi

acl_e2e_kc update "clients/${TARGET_UUID}/management/permissions" \
  -r "${REALM}" -s enabled=true >/dev/null
TOKEN_EXCHANGE_PERM_ID=$(acl_e2e_kc get "clients/${TARGET_UUID}/management/permissions" -r "${REALM}" \
  | grep '"token-exchange"' | sed -E 's/.*"token-exchange" *: *"([^"]+)".*/\1/')
if [[ -z "${TOKEN_EXCHANGE_PERM_ID}" ]]; then
  echo "❌ could not resolve the token-exchange scope-permission id for ${TOKEN_EXCHANGE_TARGET_CLIENT}." >&2
  exit 1
fi
echo "  ✓ token-exchange permission id: ${TOKEN_EXCHANGE_PERM_ID}"

TOKEN_EXCHANGE_POLICY_ID=$(find_policy_id "${TOKEN_EXCHANGE_POLICY_NAME}" "${REALM_MGMT_UUID}")
if [[ -n "${TOKEN_EXCHANGE_POLICY_ID}" ]]; then
  echo "  ⊝ policy ${TOKEN_EXCHANGE_POLICY_NAME}: exists (${TOKEN_EXCHANGE_POLICY_ID})"
else
  acl_e2e_kc create "clients/${REALM_MGMT_UUID}/authz/resource-server/policy/client" \
    -r "${REALM}" \
    -s "name=${TOKEN_EXCHANGE_POLICY_NAME}" \
    -s "clients=[\"${SOURCE_UUID}\"]" >/dev/null
  TOKEN_EXCHANGE_POLICY_ID=$(find_policy_id "${TOKEN_EXCHANGE_POLICY_NAME}" "${REALM_MGMT_UUID}")
  if [[ -z "${TOKEN_EXCHANGE_POLICY_ID}" ]]; then
    echo "❌ policy ${TOKEN_EXCHANGE_POLICY_NAME}: created but id not resolvable" >&2
    exit 1
  fi
  echo "  ✓ policy ${TOKEN_EXCHANGE_POLICY_NAME}: created (${TOKEN_EXCHANGE_POLICY_ID})"
fi

acl_e2e_kc update "clients/${REALM_MGMT_UUID}/authz/resource-server/permission/scope/${TOKEN_EXCHANGE_PERM_ID}" \
  -r "${REALM}" -s "policies=[\"${TOKEN_EXCHANGE_POLICY_ID}\"]" >/dev/null
echo "  ✓ ${TOKEN_EXCHANGE_SOURCE_CLIENT} authorized to exchange for ${TOKEN_EXCHANGE_TARGET_CLIENT}"

echo "✅ memory-scopes provisioning complete."
