#!/usr/bin/env bash
# One throwaway Keycloak with the repository's realm file: measure what the token endpoint answers
# when it refuses a token exchange, rewrite tests/fixtures/keycloak_exchange_refusals.json, remove.
# usage: kc-run.sh <scratch dir> <export dir> <locked venv python> <out.json>   (never prints the admin password)
set -euo pipefail
scratch="$1"; export_dir="$2"; py="$3"; out="$4"; name=bffcre-a1-kc; port=18097
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
record="${out%.json}.container-run.txt"
avail=$(free -g | awk '/^Mem/{print $7}')
echo "available_gb=${avail}" > "${record}"
[ "${avail}" -ge 11 ] || { echo "below the 11 GB floor: ${avail}"; exit 1; }
mkdir -p "${scratch}/import"
cp "${export_dir}/keycloak/realm-audittrace.json" "${scratch}/import/realm-audittrace.json"
KC_ADMIN_PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
export KC_ADMIN_PW KEYCLOAK_ADMIN=admin KEYCLOAK_ADMIN_PASSWORD="${KC_ADMIN_PW}"
docker rm -f "${name}" >/dev/null 2>&1 || true
trap 'docker rm -f "${name}" >/dev/null 2>&1 && echo "container=removed" >> "${record}"' EXIT
docker run -d --name "${name}" --memory 1500m -p "127.0.0.1:${port}:8080" -e KEYCLOAK_ADMIN -e KEYCLOAK_ADMIN_PASSWORD \
  -v "${scratch}/import:/opt/keycloak/data/import:ro" quay.io/keycloak/keycloak:24.0 \
  start-dev --import-realm --features=token-exchange,admin-fine-grained-authz >/dev/null
for _ in $(seq 1 90); do
  [ "$("${py}" -c "import urllib.request,sys
try: print(urllib.request.urlopen('http://127.0.0.1:${port}/realms/audittrace', timeout=3).status)
except Exception: print(0)")" = 200 ] && break
  "${py}" -c "import time; time.sleep(2)"
done
{ echo "image_id=$(docker image inspect quay.io/keycloak/keycloak:24.0 --format '{{.Id}}')"
  echo "command=start-dev --import-realm --features=token-exchange,admin-fine-grained-authz"
  echo "realm_file=keycloak/realm-audittrace.json of the export"
  echo "memory_cap=1500m"; } >> "${record}"
"${py}" "${here}/measure_exchange_refusals.py" "http://127.0.0.1:${port}" "${export_dir}" "${out}"
"${py}" "${here}/write_exchange_refusals_fixture.py" "${out}" "${record}" "${export_dir}/tests/fixtures/keycloak_exchange_refusals.json"
