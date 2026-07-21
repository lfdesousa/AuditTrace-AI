#!/usr/bin/env bash
# Post-deploy verification gate — Phase C.12.
#
# Run AFTER `make k8s-rolling-image` (or any helm upgrade) to assert
# the cluster is in a known-good state. Designed for the M5 off-LAN
# rehearsal (2026-05-15) where we need a one-shot green/red answer
# rather than eyeballing kubectl + Tempo + Loki manually.
#
# Each check prints PASS / FAIL / SKIP with a one-line reason. Final
# exit code is 0 only if ZERO checks failed (SKIPs do not fail the
# gate — they downgrade confidence but don't block).
#
# Exit codes:
#   0   — all checks passed (some may have skipped)
#   1   — environment problem (no kubectl, no helm, can't reach cluster)
#   2   — at least one check FAILED — cluster is NOT in expected state

set -euo pipefail

NAMESPACE="${NAMESPACE:-audittrace}"
RELEASE="${RELEASE:-audittrace}"
TEMPO_URL="${TEMPO_URL:-http://192.168.1.231:3200}"
LOKI_URL="${LOKI_URL:-http://192.168.1.231:3100}"
# 50 is generous for a post-deploy window: a healthy cluster typically
# emits a handful of ERROR lines from boot-time Istio sidecar races and
# a chart upgrade can briefly multiply that. A real disaster lands in
# the hundreds. Operators wanting tighter monitoring set the env var.
LOKI_ERROR_THRESHOLD="${LOKI_ERROR_THRESHOLD:-50}"
KUBECONFIG_FLAG=""
if [ -n "${KUBECONFIG:-}" ]; then
    KUBECONFIG_FLAG="--kubeconfig=$KUBECONFIG"
elif [ -f "$HOME/.kube/config" ]; then
    KUBECONFIG_FLAG="--kubeconfig=$HOME/.kube/config"
fi

PASS=0
FAIL=0
SKIP=0

pass()   { echo "[verify]  ✓ $1"; PASS=$((PASS+1)); }
fail()   { echo "[verify]  ✗ $1" >&2; FAIL=$((FAIL+1)); }
skip()   { echo "[verify]  · $1 (SKIP)"; SKIP=$((SKIP+1)); }
header() { echo "[verify]"; echo "[verify] $1"; }

# ── 0. environment ──────────────────────────────────────────────────────────
for tool in kubectl helm; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[verify] ERROR: $tool not on PATH (exit 1)" >&2
        exit 1
    fi
done

if ! kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pods -o name >/dev/null 2>&1; then
    echo "[verify] ERROR: cannot reach cluster / namespace $NAMESPACE (exit 1)" >&2
    exit 1
fi

echo "[verify] === audittrace post-deploy verification ==="
echo "[verify] namespace=$NAMESPACE release=$RELEASE"

# ── 1. All chart pods Ready ──────────────────────────────────────────────────
header "(1/11) Pod readiness"
not_ready=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pods --no-headers 2>/dev/null \
    | awk '{
        split($2, ready, "/")
        if (ready[1] != ready[2] && $3 != "Completed") print $0
      }')
if [ -z "$not_ready" ]; then
    pass "all pods Ready (or Completed)"
else
    fail "pods not Ready:"
    echo "$not_ready" | sed 's/^/[verify]      /' >&2
fi

# ── 2. No CrashLoopBackOff or Error pods ────────────────────────────────────
header "(2/11) No crashing pods"
crashing=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pods --no-headers 2>/dev/null \
    | awk '$3 == "CrashLoopBackOff" || $3 == "Error" || $3 == "ErrImagePull" {print}')
if [ -z "$crashing" ]; then
    pass "no CrashLoopBackOff / Error / ErrImagePull"
else
    fail "crashing pods:"
    echo "$crashing" | sed 's/^/[verify]      /' >&2
fi

# ── 3. Helm release status `deployed` ───────────────────────────────────────
header "(3/11) Helm release status"
release_status=$(helm $KUBECONFIG_FLAG status "$RELEASE" -n "$NAMESPACE" \
    -o json 2>/dev/null | jq -r '.info.status // "unknown"')
if [ "$release_status" = "deployed" ]; then
    pass "release '$RELEASE' status=deployed"
else
    fail "release '$RELEASE' status=$release_status (expected: deployed)"
fi

# ── 4. Memory-server /health returns 200 ────────────────────────────────────
header "(4/11) Memory-server /health"
ms_pod=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pod \
    -l app.kubernetes.io/component=memory-server \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -z "$ms_pod" ]; then
    fail "no memory-server pod found"
elif kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec "$ms_pod" -c memory-server \
        -- curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/health 2>/dev/null \
        | grep -q "^200$"; then
    pass "memory-server /health returned 200"
else
    fail "memory-server /health did not return 200"
fi

# ── 5. Memory-server /metrics reachable ─────────────────────────────────────
header "(5/11) Memory-server /metrics"
if [ -n "$ms_pod" ] && kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec "$ms_pod" \
        -c memory-server \
        -- curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/metrics 2>/dev/null \
        | grep -qE "^(200|401)$"; then
    # 401 is acceptable: /metrics is auth-gated; the endpoint IS reachable.
    pass "memory-server /metrics endpoint reachable"
else
    fail "memory-server /metrics not reachable"
fi

# ── 6. Postgres reachable (pg_isready from inside the pg pod) ───────────────
header "(6/11) Postgres reachability"
if kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec audittrace-postgresql-0 \
        -c postgresql -- pg_isready -U postgres -d audittrace 2>&1 \
        | grep -q "accepting connections"; then
    pass "postgres accepting connections"
else
    fail "postgres pg_isready failed"
fi

# ── 7. Recent Tempo trace activity for audittrace-server ────────────────────
header "(7/11) Tempo: recent traces for audittrace-server"
# 30-min window; if nothing is using the system, this can legitimately be
# empty — flag that as SKIP rather than FAIL so a quiet cluster passes.
if ! curl --silent --connect-timeout 3 --max-time 10 \
        "${TEMPO_URL}/api/echo" >/dev/null 2>&1; then
    skip "Tempo unreachable at ${TEMPO_URL}"
else
    end=$(date +%s)
    start=$((end - 1800))
    found=$(curl --silent --max-time 15 \
        "${TEMPO_URL}/api/search?tags=service.name%3Daudittrace-server&start=${start}&end=${end}&limit=1" \
        2>/dev/null | jq -r '.traces | length // 0')
    if [ "$found" = "0" ] || [ -z "$found" ]; then
        skip "no traces in last 30 min (cluster may be idle)"
    else
        pass "found $found+ recent audittrace-server traces"
    fi
fi

# ── 8. Loki: ERROR-level audittrace lines below threshold ───────────────────
header "(8/11) Loki: audittrace ERROR rate"
if ! curl --silent --connect-timeout 3 --max-time 10 \
        "${LOKI_URL}/ready" >/dev/null 2>&1; then
    skip "Loki unreachable at ${LOKI_URL}"
else
    end_ns=$(date +%s)000000000
    start_ns=$(($(date +%s) - 1800))000000000
    # Count audittrace-namespaced ERROR lines (LogQL `count_over_time`).
    err_count=$(curl --silent --max-time 15 -G "${LOKI_URL}/loki/api/v1/query" \
        --data-urlencode 'query=count_over_time({namespace="audittrace"} |= "ERROR" [30m])' \
        --data-urlencode "time=${end_ns}" 2>/dev/null \
        | jq -r '[.data.result[].value[1] // "0"] | map(tonumber) | add // 0')
    err_count=${err_count:-0}
    if [ "$err_count" -le "$LOKI_ERROR_THRESHOLD" ]; then
        pass "Loki ERROR count over 30m = $err_count (threshold $LOKI_ERROR_THRESHOLD)"
    else
        fail "Loki ERROR count over 30m = $err_count (threshold $LOKI_ERROR_THRESHOLD exceeded)"
    fi
fi

# ── 9. Vault drift guard (ConfigMap policies/roles ⊆ actual Vault state) ───
header "(9/11) Vault drift guard (ConfigMap ⊆ Vault)"
# Catches the 2026-05-03 drift class: chart adds a policy/role to
# templates/vault/configmap-policies.yaml, operator forgets to re-run
# `make k8s-bootstrap-secrets`, vault-agent fails authn at the next pod
# rollout. Any expected entry missing from Vault is a hard FAIL with a
# concrete diff. Reuses the same go-template + kubectl-exec pattern as
# scripts/setup-vault.sh so there is one source of truth for "expected".
#
# SKIPped when:
#   - VAULT_TOKEN is unset (drift guard requires a Vault token to list
#     policies/roles; cluster-recovery scenarios run the gate first to
#     confirm the rest of the chart is healthy, then operator re-runs
#     once a token is in hand)
#   - vault-0 pod is not Ready (cold-start)
#   - the policies ConfigMap is absent (vault.enabled=false in chart)
POLICIES_CM="${RELEASE}-vault-policies"
vault_pod="${RELEASE}-vault-0"
vault_ready=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pod "$vault_pod" \
    -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || echo "false")

if [ -z "${VAULT_TOKEN:-}" ]; then
    skip "VAULT_TOKEN unset — drift guard requires a Vault token"
elif [ "$vault_ready" != "true" ]; then
    skip "vault-0 pod not Ready (status=$vault_ready)"
elif ! kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get configmap "$POLICIES_CM" \
        >/dev/null 2>&1; then
    skip "ConfigMap $POLICIES_CM not present (vault.enabled=false?)"
else
    # Expected from ConfigMap (mirrors setup-vault.sh:cm_keys_matching).
    expected_policies=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" \
        get configmap "$POLICIES_CM" \
        -o go-template='{{range $k, $_ := .data}}{{$k}}{{"\n"}}{{end}}' \
        | grep -E '\.hcl$' | sed 's/\.hcl$//' | sort)
    expected_roles=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" \
        get configmap "$POLICIES_CM" \
        -o go-template='{{range $k, $_ := .data}}{{$k}}{{"\n"}}{{end}}' \
        | grep -E '^role-.*\.env$' | sed -E 's/^role-(.*)\.env$/\1/' | sort)

    # Actual from Vault — `vault list -format=json` returns a JSON array
    # of strings. Empty mounts return null/empty; jq's `.[]?` is null-safe.
    actual_policies=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec "$vault_pod" -- \
        env "VAULT_TOKEN=${VAULT_TOKEN}" \
        vault list -format=json sys/policies/acl 2>/dev/null \
        | jq -r '.[]?' 2>/dev/null | sort || echo "")
    actual_roles=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec "$vault_pod" -- \
        env "VAULT_TOKEN=${VAULT_TOKEN}" \
        vault list -format=json auth/kubernetes/role 2>/dev/null \
        | jq -r '.[]?' 2>/dev/null | sort || echo "")

    # comm -23 prints lines unique to first input (expected but not actual).
    # `|| true` because a non-empty diff still exits 0; we use the output
    # to decide pass/fail.
    missing_policies=$(comm -23 <(echo "$expected_policies") <(echo "$actual_policies") \
        | grep -v '^$' || true)
    missing_roles=$(comm -23 <(echo "$expected_roles") <(echo "$actual_roles") \
        | grep -v '^$' || true)

    if [ -z "$missing_policies" ] && [ -z "$missing_roles" ]; then
        pol_count=$(echo "$expected_policies" | grep -c -v '^$' || echo 0)
        role_count=$(echo "$expected_roles" | grep -c -v '^$' || echo 0)
        pass "Vault has all $pol_count expected policies and $role_count expected roles"
    else
        fail "Vault drift detected — run 'make k8s-bootstrap-secrets':"
        if [ -n "$missing_policies" ]; then
            echo "[verify]      missing policies (in ConfigMap, not in Vault):" >&2
            echo "$missing_policies" | sed 's/^/[verify]        - /' >&2
        fi
        if [ -n "$missing_roles" ]; then
            echo "[verify]      missing roles (in ConfigMap, not in Vault):" >&2
            echo "$missing_roles" | sed 's/^/[verify]        - /' >&2
        fi
    fi
fi

# ── 10. Vault ↔ k8s Redis password alignment (closes 2026-05-04 drift) ─────
header "(10/11) Vault Redis-password sync"
# v1.0.9 ADR-046 live test surfaced this drift class: Bitnami Redis
# subchart auto-generates the password into the k8s secret
# '${RELEASE}-redis' on first install; setup-vault.sh independently
# seeded Vault from secrets/redis_password.txt (a different value).
# Memory-server with vault.enabled=true read Vault → couldn't auth.
# v1.0.10 ``setup-vault.sh`` now syncs Vault from the k8s secret on
# every ``make k8s-bootstrap-secrets`` run; this check is the gate that
# notices when the sync hasn't happened (or someone manually rewrote
# Vault between bootstraps).
#
# SKIP semantics mirror check 9: VAULT_TOKEN unset, vault-0 not Ready.
if [ -z "${VAULT_TOKEN:-}" ]; then
    skip "VAULT_TOKEN unset — Redis password sync check requires Vault token"
elif [ "$vault_ready" != "true" ]; then
    skip "vault-0 pod not Ready (status=$vault_ready)"
else
    k8s_pw=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get secret \
              "${RELEASE}-redis" -o jsonpath='{.data.redis-password}' \
              2>/dev/null | base64 -d 2>/dev/null || echo "")
    vault_pw=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec "$vault_pod" -- \
                env "VAULT_TOKEN=${VAULT_TOKEN}" \
                vault kv get -field=password kv/audittrace/redis/main \
                2>/dev/null || echo "")
    if [ -z "$k8s_pw" ]; then
        skip "k8s secret '${RELEASE}-redis' not found (chart not installed?)"
    elif [ -z "$vault_pw" ]; then
        fail "Vault kv/audittrace/redis/main is empty — run 'make k8s-bootstrap-secrets'"
    elif [ "$k8s_pw" = "$vault_pw" ]; then
        pass "Redis password aligned: k8s '${RELEASE}-redis' matches Vault kv/audittrace/redis/main"
    else
        fail "Redis password drift — k8s '${RELEASE}-redis' != Vault kv/audittrace/redis/main. Run 'make k8s-bootstrap-secrets'"
    fi
fi

# ── 11. Keycloak realm scope drift (ConfigMap declared == live realm) ──────
header "(11/11) Keycloak client-scope drift (declared == live)"
# Catches the 2026-07-20 drift class (#370): the live realm granted
# `memory:episodic:write` as a DEFAULT scope on audittrace-opencode, while
# every declared source (both realm JSON files, both provisioning scripts,
# and their whole git history) said OPTIONAL. Every human identity on the
# Device Flow client was therefore receiving a WRITE scope in every token
# without asking for it.
#
# Why nothing caught it: Keycloak's `--import-realm` runs on FIRST BOOT
# ONLY. After the realm exists, the ConfigMap is inert — an edit to
# realm-audittrace.json changes what a FRESH cluster would get and has no
# effect on this one. tests/test_chart_drift_guards.py compares file to
# file and structurally cannot see the live realm. This check is the only
# place the two can be compared.
#
# Expected comes from the DEPLOYED ConfigMap rather than the working
# tree, so the gate answers "does the cluster match what was shipped to
# it" and stays correct when run from outside a git checkout.
#
# BOTH directions are reported, because they fail differently:
#   over-privileged  (live default ⊅ declared)  — the security bug. A scope
#                    nobody asked for lands in every token.
#   under-privileged (declared default ⊄ live)  — the availability bug.
#                    Callers that never had to ask now get 403.
#
# SKIPped when: the realm ConfigMap is absent (keycloak.enabled=false), no
# admin credential is available, or the token request fails. A missing
# credential must not fail the gate — unprivileged post-deploy runs are a
# supported mode (mirrors checks 9/10).
REALM_CM="${RELEASE}-keycloak-realm"
KC_SVC="${KC_SVC:-http://${RELEASE}-keycloak:8080}"
KC_REALM="${KC_REALM:-audittrace}"

# Credential resolution order: explicit env, then the chart's secret (which
# only exists when keycloak.auth is not Vault-backed), then give up.
kc_admin_pw="${KEYCLOAK_ADMIN_PASSWORD:-}"
if [ -z "$kc_admin_pw" ]; then
    kc_admin_pw=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get secret \
        "${RELEASE}-keycloak-secret" -o jsonpath='{.data.admin-password}' \
        2>/dev/null | base64 -d 2>/dev/null || echo "")
fi

if ! kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get configmap "$REALM_CM" \
        >/dev/null 2>&1; then
    skip "ConfigMap $REALM_CM not present (keycloak.enabled=false?)"
elif [ -z "$ms_pod" ]; then
    skip "no memory-server pod to reach Keycloak from"
elif [ -z "$kc_admin_pw" ]; then
    skip "no Keycloak admin credential (set KEYCLOAK_ADMIN_PASSWORD)"
else
    # The password goes in on STDIN, never in argv: `kubectl exec -- env
    # VAR=secret` publishes the value to the pod's process table, where any
    # other process in that container can read it from /proc. Checks 9/10
    # predate this and still use the env form; new code should not.
    kc_curl() {  # kc_curl <curl-args...> — runs inside the memory-server pod
        kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec "$ms_pod" \
            -c memory-server -- curl -s --max-time 15 "$@" 2>/dev/null || true
    }
    kc_token=$(printf '%s' "$kc_admin_pw" \
        | kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec -i "$ms_pod" \
            -c memory-server -- sh -c "read -r PW; curl -s --max-time 15 \
              -X POST '${KC_SVC}/realms/master/protocol/openid-connect/token' \
              -d grant_type=password -d client_id=admin-cli -d username=admin \
              --data-urlencode \"password=\$PW\"" 2>/dev/null \
        | jq -r '.access_token // empty' 2>/dev/null || echo "")

    if [ -z "$kc_token" ]; then
        skip "Keycloak admin auth failed (bad credential, or Keycloak not ready)"
    else
        realm_json=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get configmap \
            "$REALM_CM" -o jsonpath='{.data.realm\.json}' 2>/dev/null || echo "")

        # SECOND declared source. The realm JSON is not the whole story: the
        # ensure-memory-scopes Job binds its own SCOPES list to specific
        # clients (CLIENT_KIND decides default vs optional) precisely BECAUSE
        # --import-realm is inert after first boot. Those bindings are
        # intentional and are legitimately present live while absent from the
        # realm JSON.
        #
        # Ignoring the Job would make this check fail permanently on a
        # CORRECT cluster — admin-client alone would report three phantom
        # over-privileges forever. A guard that cries wolf gets muted, and a
        # muted guard is worse than none: #370 got through while a green gate
        # was running. So expected = realm JSON UNION the Job's intent.
        scopes_cm="${RELEASE}-memory-scopes-script"
        job_script=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get configmap \
            "$scopes_cm" -o jsonpath='{.data.*}' 2>/dev/null || echo "")
        job_scopes=$(echo "$job_script" | sed -n '/SCOPES=(/,/^ *)/p' \
            | grep -oE '"[^"]+"' | tr -d '"' | sort || echo "")
        # Only clients the ConfigMap actually declares scope sets for. No
        # hardcoded client list — a new client in the chart is covered the
        # day it ships, with no edit here.
        clients=$(echo "$realm_json" \
            | jq -r '.clients[]? | select(.defaultClientScopes != null) | .clientId' \
            2>/dev/null | sort || echo "")
        scope_drift=""
        checked=0

        for cid in $clients; do
            live_uuid=$(kc_curl -H "Authorization: Bearer $kc_token" \
                "${KC_SVC}/admin/realms/${KC_REALM}/clients?clientId=${cid}" \
                | jq -r '.[0].id // empty' 2>/dev/null || echo "")
            [ -z "$live_uuid" ] && continue   # declared but not deployed
            checked=$((checked + 1))

            for kind in default optional; do
                declared=$(echo "$realm_json" | jq -r \
                    --arg c "$cid" --arg k "${kind}ClientScopes" \
                    '.clients[]|select(.clientId==$c)|.[$k][]?' 2>/dev/null \
                    | sort || echo "")
                # Fold in the Job's intent when it targets THIS client with
                # THIS kind. grep -F -x so a scope name is never treated as
                # a regex (they contain ':' but a future one may not).
                if echo "$job_script" \
                     | grep -qF "CLIENT_KIND[\"${cid}\"]=\"${kind}\"" 2>/dev/null; then
                    declared=$(printf '%s\n%s\n' "$declared" "$job_scopes" \
                        | grep -v '^$' | sort -u || echo "")
                fi
                live=$(kc_curl -H "Authorization: Bearer $kc_token" \
                    "${KC_SVC}/admin/realms/${KC_REALM}/clients/${live_uuid}/${kind}-client-scopes" \
                    | jq -r '.[].name' 2>/dev/null | sort || echo "")

                # comm -13 = in live only (extra); -23 = declared only (missing).
                extra=$(comm -13 <(echo "$declared") <(echo "$live") | grep -v '^$' || true)
                missing=$(comm -23 <(echo "$declared") <(echo "$live") | grep -v '^$' || true)

                for s in $extra; do
                    if [ "$kind" = "default" ]; then
                        scope_drift="${scope_drift}${cid}: OVER-PRIVILEGED — '$s' is live-default but not declared-default"$'\n'
                    else
                        scope_drift="${scope_drift}${cid}: '$s' is live-optional but not declared-optional"$'\n'
                    fi
                done
                for s in $missing; do
                    scope_drift="${scope_drift}${cid}: '$s' is declared-${kind} but MISSING from live ${kind}"$'\n'
                done
            done
        done

        # ── UNDECLARED ("shadow") clients ───────────────────────────────
        # The loop above walks DECLARED -> LIVE, so a client that exists live
        # but is declared nowhere was never enumerated and never reported.
        # Found 2026-07-20 (#371): audittrace-restricted was created via the
        # admin API and this check passed it in SILENCE. Not "checked and
        # clean" — not checked.
        #
        # It matters more than the scope comparison it complements. The drift
        # this check was built for (#370) was someone CHANGING a scope on an
        # existing client. The strictly worse move is CREATING a client with
        # audittrace:admin as a default scope — and that is exactly the shape
        # a declared->live walk cannot see. It is also the more attractive
        # move, because it disturbs nothing already being watched.
        #
        # The allowlist is EXACT NAMES ONLY. A prefix rule such as
        # "audittrace-*" would exempt precisely the clients most worth
        # watching, which is the opposite of the point. Keycloak creates these
        # six in every realm; everything else must be declared.
        KC_BUILTIN_CLIENTS="account
account-console
admin-cli
broker
realm-management
security-admin-console"
        live_clients=$(kc_curl -H "Authorization: Bearer $kc_token" \
            "${KC_SVC}/admin/realms/${KC_REALM}/clients" \
            | jq -r '.[].clientId' 2>/dev/null | sort || echo "")
        undeclared=$(comm -23 <(echo "$live_clients" | grep -v '^$') \
                              <(printf '%s\n%s\n' "$clients" "$KC_BUILTIN_CLIENTS" \
                                | grep -v '^$' | sort -u) || true)

        # Rank by blast radius: a shadow client holding admin or audit as a
        # DEFAULT scope is a different incident from one holding neither, and
        # an operator triaging a red gate needs that distinction up front.
        shadow_report=""
        for cid in $undeclared; do
            u=$(kc_curl -H "Authorization: Bearer $kc_token" \
                "${KC_SVC}/admin/realms/${KC_REALM}/clients?clientId=${cid}" \
                | jq -r '.[0].id // empty' 2>/dev/null || echo "")
            sc=$(kc_curl -H "Authorization: Bearer $kc_token" \
                "${KC_SVC}/admin/realms/${KC_REALM}/clients/${u}/default-client-scopes" \
                | jq -r '[.[].name]|join(",")' 2>/dev/null || echo "")
            case ",$sc," in
                *,audittrace:admin,*|*,audittrace:audit,*)
                    shadow_report="${shadow_report}!! ${cid}: UNDECLARED and holds a privileged default scope [${sc}]"$'\n' ;;
                *)
                    shadow_report="${shadow_report}${cid}: UNDECLARED (default scopes: ${sc:-none})"$'\n' ;;
            esac
        done

        if [ "$checked" -eq 0 ]; then
            skip "no declared clients found in $REALM_CM"
        elif [ -z "$scope_drift" ] && [ -z "$shadow_report" ]; then
            pass "Keycloak client scopes match $REALM_CM for all $checked client(s); no undeclared clients"
        elif [ -z "$scope_drift" ]; then
            fail "Keycloak realm has UNDECLARED client(s) — present live, declared nowhere:"
            echo "$shadow_report" | grep -v '^$' | sed 's/^/[verify]      - /' >&2
            echo "[verify]      A client declared nowhere is unreviewed. Either add it" >&2
            echo "[verify]      to the realm ConfigMap or delete it from the realm." >&2
        else
            fail "Keycloak realm scope drift — live realm != $REALM_CM:"
            echo "$scope_drift" | grep -v '^$' | sed 's/^/[verify]      - /' >&2
            if [ -n "$shadow_report" ]; then
                echo "[verify]      PLUS undeclared client(s):" >&2
                echo "$shadow_report" | grep -v '^$' | sed 's/^/[verify]      - /' >&2
            fi
            echo "[verify]      NOTE: --import-realm runs on FIRST BOOT ONLY, so" >&2
            echo "[verify]      editing realm-audittrace.json will NOT fix a running" >&2
            echo "[verify]      cluster. Correct the live realm via the admin API." >&2
        fi
    fi
fi
unset kc_admin_pw

# ── Summary ─────────────────────────────────────────────────────────────────
echo "[verify]"
echo "[verify] ─────────────────────────────────────────"
echo "[verify]  Summary:  $PASS passed | $FAIL failed | $SKIP skipped"
echo "[verify] ─────────────────────────────────────────"

if [ "$FAIL" -gt 0 ]; then
    echo "[verify] gate FAILED — cluster is not in expected state" >&2
    exit 2
fi
echo "[verify] gate PASSED"
exit 0
