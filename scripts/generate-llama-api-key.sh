#!/usr/bin/env bash
# LC-5 / M3-WU-4 -- generate the shared llama API key file.
#
# Recipe (spec ADDENDUM 3 S2, measured 200/200): draw 96 random bytes, base64,
# keep only [A-Za-z0-9], cut to 48. A 32-byte draw yields only 38-43
# alphanumerics, which is why the draw is 96. The key lands inside a shell
# `export X='...'` Vault Agent template (PM-4), so no quote or shell
# character may appear. Asserts ^[A-Za-z0-9]{48}$ and exits non-zero
# otherwise. NEVER overwrites an existing file, NEVER prints the key.
#
# Usage: generate-llama-api-key.sh <key file>
set -euo pipefail

target="${1:?usage: $0 <key file>}"
if [[ -e "$target" ]]; then
  echo "error: $target already exists; refusing to overwrite a provisioned key" >&2
  exit 1
fi

# pipefail off inside the substitution: `head` closing the pipe early may
# SIGPIPE `tr`; the regex assertion below is the real check.
key="$(set +o pipefail; head -c 96 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48)"
if [[ ! "$key" =~ ^[A-Za-z0-9]{48}$ ]]; then
  echo "error: key recipe did not yield [A-Za-z0-9]{48}" >&2
  exit 1
fi

install -d -m 0700 "$(dirname "$target")"
( umask 077 && printf '%s\n' "$key" > "$target" )
echo "wrote $target (mode 0600; value not printed)"
