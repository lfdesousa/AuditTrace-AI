#!/usr/bin/env bash
# One-time operator setup — make local Node-based clients (OpenCode,
# Continue, Roo Code) trust the cluster's self-signed Istio Gateway cert
# without resorting to NODE_TLS_REJECT_UNAUTHORIZED=0.
#
# Why this is needed:
#   - The chart's secret-tls.yaml mints a self-signed cert with SAN
#     audittrace.local + localhost. Browsers / curl can be told to trust
#     it via --cacert.
#   - Node has its OWN bundled CA list (Mozilla) and ignores both the
#     system CA store and curl's -k flag. The standard escape hatch is
#     NODE_EXTRA_CA_CERTS pointing at a PEM file with the extra trust
#     anchor.
#
# What this script does (idempotent):
#   1. Extract the cert from the cluster Secret istio-system/audittrace-tls
#      to ~/.config/audittrace/ca.crt (mode 644).
#   2. Print the line the operator must add to their shell rc
#      (we DO NOT modify shell rc files automatically — that surface is
#      operator-private).
#
# Re-run after:
#   - Cert rotation (new SAN list, new validity window).
#   - First install on a new laptop.

set -euo pipefail

KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
NS="${ISTIO_GATEWAY_NAMESPACE:-istio-system}"
SECRET="${ISTIO_GATEWAY_SECRET:-audittrace-tls}"
DEST_DIR="${AUDITTRACE_TRUST_DIR:-$HOME/.config/audittrace}"
DEST="${DEST_DIR}/ca.crt"

mkdir -p "${DEST_DIR}"

echo "🔐 Extracting cluster cert from ${NS}/${SECRET}..."
kubectl --kubeconfig="${KUBECONFIG}" -n "${NS}" get secret "${SECRET}" \
    -o jsonpath='{.data.tls\.crt}' | base64 -d > "${DEST}"
chmod 644 "${DEST}"

echo "  ✓ wrote ${DEST}"

echo
echo "Cert SAN:"
openssl x509 -in "${DEST}" -text -noout | grep -A 1 'Subject Alternative Name' | sed 's/^/    /'
echo

if [[ "${NODE_EXTRA_CA_CERTS:-}" == "${DEST}" ]]; then
  echo "✅ NODE_EXTRA_CA_CERTS already points at ${DEST}. You're set."
  exit 0
fi

echo "Add this line to your shell rc (~/.zshrc or ~/.bashrc) to make"
echo "OpenCode / Continue / Roo Code trust the cluster cert:"
echo
echo "    export NODE_EXTRA_CA_CERTS=\"${DEST}\""
echo
echo "Then reload your shell or run:"
echo
echo "    export NODE_EXTRA_CA_CERTS=\"${DEST}\""
echo
echo "Smoke test (should print HTTP 200 — no -k, no NODE_TLS_REJECT_UNAUTHORIZED):"
echo
echo "    NODE_EXTRA_CA_CERTS=\"${DEST}\" node -e 'fetch(\"https://audittrace.local:30952/health\").then(r=>console.log(r.status))'"
