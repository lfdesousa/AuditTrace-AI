"""IdP-brokering export transform for SPEC #403.

``scripts/export-idp-federation.sh`` (operator-run, needs a live Keycloak) is
a thin orchestration shell around this module: it shells out to ``kcadm.sh``
to fetch the live realm's ``identity-provider/instances`` +
``identity-provider/instances/<alias>/mappers``, then pipes the raw kcadm
JSON through :func:`build_export_payload` here to produce the form that is
safe to commit into ``keycloak/realm-audittrace.json`` and
``charts/audittrace/files/realm-audittrace.json``.

The one property this module exists to guarantee: **no plaintext IdP client
secret ever reaches the committed payload.** kcadm's
``identity-provider/instances`` response carries the live
``config.clientSecret`` in the clear (see ``scripts/setup-idp-federation.sh``,
which POSTs the same field on the way in). :func:`externalize_idp_secrets`
replaces it with the Vault-substitution placeholder documented in
ADR-044 §3 (``${vault:idp/<alias>/client_secret}``, resolved against
``kv/audittrace/idp/<alias>/client_secret`` per ADR-043 §5's
``kv/audittrace/<service>/<key>`` convention) before the payload is ever
written to a file a `git add` could reach.
:func:`assert_no_plaintext_client_secret` is the belt-and-suspenders check
that runs on the way OUT of :func:`build_export_payload` too, so a caller
that skips :func:`externalize_idp_secrets` (or a future edit that reorders
the pipeline) still cannot produce a payload with a raw secret in it.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from typing import Any, TextIO

logger = logging.getLogger(__name__)

# ADR-044 §3's documented placeholder shape, verbatim. Do not invent a new
# syntax here — the realm-import resolution mechanism (documented in
# ADR-044's #403 follow-up) is written against this exact string shape.
_VAULT_PLACEHOLDER_TEMPLATE = "${{vault:idp/{alias}/client_secret}}"

# Keycloak config keys that carry the live IdP's client secret. Only
# ``clientSecret`` exists on the OIDC provider shape today (ADR-044 §3);
# kept as a tuple so a future SAML/social provider field is a one-line add.
_SECRET_CONFIG_KEYS: tuple[str, ...] = ("clientSecret",)


def externalize_idp_secrets(
    identity_providers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a deep copy of ``identity_providers`` with every client secret
    replaced by its Vault-substitution placeholder.

    ``identity_providers`` is the shape kcadm's
    ``GET identity-provider/instances`` returns: a list of Keycloak IdP
    instance objects, each with an ``alias`` and a ``config`` map. Any
    ``config`` key listed in :data:`_SECRET_CONFIG_KEYS` that carries a
    non-empty value is replaced; every other field passes through
    unmodified so the export stays faithful to the live config.

    Raises:
        ValueError: an entry has no ``alias`` — a Vault path cannot be
            built for it, and committing a secret-bearing entry under an
            unknown alias is worse than refusing.
    """
    externalized: list[dict[str, Any]] = []
    for idp in identity_providers:
        idp_copy = copy.deepcopy(idp)
        alias = idp_copy.get("alias")
        if not alias:
            raise ValueError(
                "identity provider entry has no 'alias' — refusing to "
                "export it (cannot build a Vault path or verify secret "
                "externalization without one)"
            )
        config = idp_copy.setdefault("config", {})
        for key in _SECRET_CONFIG_KEYS:
            if config.get(key):
                config[key] = _VAULT_PLACEHOLDER_TEMPLATE.format(alias=alias)
        externalized.append(idp_copy)
    return externalized


def assert_no_plaintext_client_secret(
    identity_providers: list[dict[str, Any]],
) -> None:
    """Raise if any entry's ``config.clientSecret`` is not a Vault placeholder.

    This is the defence-in-depth check :func:`build_export_payload` runs on
    its way out. It is deliberately independent of
    :func:`externalize_idp_secrets` — a caller must not be able to bypass
    the secret-externalization guarantee by calling
    :func:`build_export_payload` with data that was never passed through
    the externalizer.
    """
    offenders = [
        idp.get("alias", "<unknown>")
        for idp in identity_providers
        if (secret := idp.get("config", {}).get("clientSecret"))
        and not secret.startswith("${vault:")
    ]
    if offenders:
        raise ValueError(
            "refusing to emit a committable payload with a non-externalized "
            f"clientSecret for: {', '.join(offenders)}. Every IdP client "
            "secret must be replaced by a ${vault:...} placeholder before "
            "it is safe to commit — see externalize_idp_secrets()."
        )


def build_export_payload(
    identity_providers: list[dict[str, Any]],
    identity_provider_mappers: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the committable ``{identityProviders, identityProviderMappers}``
    payload from raw kcadm export data.

    Applies :func:`externalize_idp_secrets` to ``identity_providers``, then
    re-verifies the result with :func:`assert_no_plaintext_client_secret`
    before returning. ``identity_provider_mappers`` (kcadm's
    ``identity-provider/instances/<alias>/mappers`` responses, concatenated
    across aliases) carries no secrets by shape — attribute-claim mappers
    per ADR-044 §4 — and passes through unmodified.
    """
    externalized = externalize_idp_secrets(identity_providers)
    assert_no_plaintext_client_secret(externalized)
    return {
        "identityProviders": externalized,
        "identityProviderMappers": list(identity_provider_mappers or []),
    }


def _find_json_array_span(text: str, key: str) -> tuple[int, int]:
    """Return the ``(start, end)`` character span of the JSON array VALUE
    for ``"<key>": [...]`` in ``text`` — ``start`` is the index of the
    opening ``[``, ``end`` is one past the matching ``]``.

    Deliberately a hand-rolled bracket-matcher rather than ``json.loads``:
    ``charts/audittrace/files/realm-audittrace.json`` embeds Go/Helm
    template directives (``{{- range ... }}``) elsewhere in the file, which
    are not valid JSON, so the document as a whole cannot be parsed. This
    function only needs to locate ONE array value precisely; everything
    else in the document — Helm directives included — is never touched.

    Raises:
        ValueError: the key is not found, or its value is not a JSON array.
    """
    marker = f'"{key}"'
    key_idx = text.find(marker)
    if key_idx == -1:
        raise ValueError(f"key {key!r} not found in document")
    colon_idx = text.index(":", key_idx + len(marker))
    i = colon_idx + 1
    while text[i] in " \t\r\n":
        i += 1
    if text[i] != "[":
        raise ValueError(f"value for key {key!r} is not a JSON array")

    start = i
    depth = 0
    in_string = False
    escape = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    raise ValueError(f"unterminated JSON array for key {key!r}")


def patch_realm_document(document_text: str, payload: dict[str, Any]) -> str:
    """Replace the ``identityProviders`` / ``identityProviderMappers`` array
    VALUES in a realm JSON document's raw text with ``payload``'s values,
    leaving every other byte of the document — Helm template directives
    included — untouched.

    This is what makes the export round-trip safe for BOTH committed realm
    files: ``keycloak/realm-audittrace.json`` (plain JSON) and
    ``charts/audittrace/files/realm-audittrace.json`` (JSON interleaved
    with Go-template directives, per the chart's redirect-URI/web-origin
    templating). A ``json.loads`` round-trip would reformat or outright
    fail on the second file; this function does neither.
    """
    result = document_text
    for key in ("identityProviderMappers", "identityProviders"):
        if key not in payload:
            continue
        start, end = _find_json_array_span(result, key)
        new_array_text = json.dumps(payload[key], indent=2)
        result = result[:start] + new_array_text + result[end:]
    return result


def _load_json_arg(value: str | None) -> Any:
    """Load a JSON argument that is either a file path or ``-`` for stdin."""
    if value is None:
        return None
    if value == "-":
        return json.load(sys.stdin)
    with open(value, encoding="utf-8") as fh:
        return json.load(fh)


def _write_output(payload: dict[str, Any], destination: str) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if destination == "-":
        sys.stdout.write(text)
    else:
        with open(destination, "w", encoding="utf-8") as fh:
            fh.write(text)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Externalize IdP client secrets from a live kcadm export into a "
            "committable JSON payload (SPEC #403)."
        )
    )
    parser.add_argument(
        "--identity-providers",
        required=True,
        help=(
            "Path to kcadm's 'identity-provider/instances' JSON array, or "
            "'-' for stdin."
        ),
    )
    parser.add_argument(
        "--mappers",
        default=None,
        help=(
            "Path to the concatenated 'identity-provider/instances/"
            "<alias>/mappers' JSON array, or '-' for stdin. Optional — "
            "omit for an IdP with no attribute mappers."
        ),
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Where to write the committable payload (default: stdout).",
    )
    parser.add_argument(
        "--patch-into",
        nargs="*",
        default=[],
        metavar="REALM_JSON_FILE",
        help=(
            "Realm JSON file(s) to patch identityProviders / "
            "identityProviderMappers into IN PLACE (text-span patch, not a "
            "full JSON round-trip — safe for the chart's Helm-templated "
            "copy). Independent of --output, which still runs."
        ),
    )
    return parser


def main(argv: list[str] | None = None, *, stderr: TextIO = sys.stderr) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        identity_providers = _load_json_arg(args.identity_providers) or []
        mappers = _load_json_arg(args.mappers) or []
        payload = build_export_payload(identity_providers, mappers)

        for target in args.patch_into:
            with open(target, encoding="utf-8") as fh:
                original = fh.read()
            patched = patch_realm_document(original, payload)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(patched)
            logger.info("patched %s", target)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=stderr)
        return 1

    _write_output(payload, args.output)
    logger.info(
        "exported %d identity provider(s), %d mapper(s)",
        len(payload["identityProviders"]),
        len(payload["identityProviderMappers"]),
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
