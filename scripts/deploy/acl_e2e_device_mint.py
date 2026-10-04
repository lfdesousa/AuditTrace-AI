"""Device-flow token minter for the dedicated ACL E2E client (WU-2c).

**E2E tooling, not deploy logic.** It lives under ``scripts/deploy/`` only
so the per-file coverage floor applies locally and in CI (the package
already owns the front-door/token seams).

Mints a human-subject access token for the ``audittrace-acl-e2e`` Keycloak
client (device grant, RFC 8628) WITHOUT touching shared identity state:

* it **refuses to run** (exit 2, no network) unless both
  ``AUDITTRACE_TOKENS_DIR`` and ``AUDITTRACE_BRUNO_ENV`` are set AND
  neither resolves under the operator's default token directory or the
  repo's ``bruno/`` collection — so a mint can never overwrite the
  orchestrator's own token store (the 2026-10-02 stamped-as-the-wrong-user
  traceability defect);
* it ALWAYS sends an explicit ``scope=`` (the #370 lesson) and persists
  only ``{access_token, access_expires_at, realm_issuer, client_id}`` —
  the refresh-token keys the login script would carry are deliberately
  absent;
* it NEVER prints a token or a device code. Stdout/stderr carry only the
  ``user_code``, the ``verification_uri_complete``, Keycloak error bodies
  reduced to ``{status, error, error_description}``, and the non-secret
  claims of the minted token (``azp``, ``scope``, ``sub``, ``sid``,
  ``iat``, ``exp``, ``exp - iat``). The ``device_code`` is a bearer
  equivalent for its lifespan and is held in memory only.

Falsifiable: each refusal and each no-leak path has an offline test that
fails when the guard is removed (``tests/test_acl_e2e_device_mint.py``).
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlencode

from scripts.deploy.memory import _http_request, ssl_context

logger = logging.getLogger(__name__)

CLIENT_ID = "audittrace-acl-e2e"
DEFAULT_SCOPE = "openid memory:acl:read-own memory:acl:write"
DEFAULT_KEYCLOAK_BASE = "https://audittrace.local:30952"
DEFAULT_REALM = "audittrace"
TOKENS_DIR_ENV = "AUDITTRACE_TOKENS_DIR"
BRUNO_ENV_ENV = "AUDITTRACE_BRUNO_ENV"
_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_REPO_ROOT = Path(__file__).resolve().parents[2]

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_DEVICE_ERROR = 3
EXIT_DENIED = 4
EXIT_EXPIRED = 5
EXIT_WRITE_FAILED = 6


class MintRefused(Exception):  # noqa: N818 - a refusal, not an error class
    """The isolation precondition failed; nothing was sent or written."""


def forbidden_roots(home: Path | None = None) -> list[Path]:
    """The shared identity stores a mint must never write under."""
    base = home if home is not None else Path.home()
    return [
        (base / ".config" / "audittrace").resolve(),
        (_REPO_ROOT / "bruno").resolve(),
    ]


def resolve_isolated_paths(
    env: Mapping[str, str], home: Path | None = None
) -> tuple[Path, Path]:
    """Return ``(tokens_dir, bruno_env_file)`` or raise :class:`MintRefused`."""
    tokens_raw = env.get(TOKENS_DIR_ENV, "").strip()
    bruno_raw = env.get(BRUNO_ENV_ENV, "").strip()
    missing = [
        name
        for name, value in ((TOKENS_DIR_ENV, tokens_raw), (BRUNO_ENV_ENV, bruno_raw))
        if not value
    ]
    if missing:
        raise MintRefused(f"{', '.join(missing)} must be set (isolation precondition)")
    tokens_dir = Path(tokens_raw).expanduser().resolve()
    bruno_file = Path(bruno_raw).expanduser().resolve()
    for label, path in ((TOKENS_DIR_ENV, tokens_dir), (BRUNO_ENV_ENV, bruno_file)):
        for root in forbidden_roots(home):
            if path == root or root in path.parents:
                raise MintRefused(f"{label} resolves under a shared identity store")
    return tokens_dir, bruno_file


def _error_summary(status: int, body: bytes) -> dict[str, object]:
    """Reduce a Keycloak error body to ``{status, error, error_description}``
    — never the raw body (it may echo a secret)."""
    error: object = None
    description: object = None
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        description = parsed.get("error_description")
    return {
        "status": status,
        "error": error if isinstance(error, str) else None,
        "error_description": description if isinstance(description, str) else None,
    }


def _form_post(
    url: str,
    form: Mapping[str, str],
    *,
    insecure: bool,
    http: Callable[..., tuple[int, dict[str, str], bytes]],
) -> tuple[int, bytes]:
    status, _headers, body = http(
        "POST",
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode(form).encode("utf-8"),
        context=ssl_context(insecure),
    )
    return status, body


def decode_claims(access_token: str) -> dict[str, object]:
    """Unverified payload decode — for DISPLAY of non-secret claims only."""
    try:
        payload = access_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def claims_summary(claims: Mapping[str, object]) -> dict[str, object]:
    """The non-secret claim record the evidence keeps per token."""
    iat = claims.get("iat")
    exp = claims.get("exp")
    lifetime = exp - iat if isinstance(iat, int) and isinstance(exp, int) else None
    return {
        "azp": claims.get("azp"),
        "scope": claims.get("scope"),
        "sub": claims.get("sub"),
        "sid": claims.get("sid"),
        "iat": iat,
        "exp": exp,
        "exp_minus_iat": lifetime,
    }


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)


def persist(
    *,
    tokens_dir: Path,
    bruno_file: Path,
    access_token: str,
    expires_in: int,
    realm_issuer: str,
    now: float,
) -> None:
    """Write the two isolated stores. No refresh-token key, by design."""
    record = {
        "access_token": access_token,
        "access_expires_at": int(now) + int(expires_in),
        "realm_issuer": realm_issuer,
        "client_id": CLIENT_ID,
    }
    _write_private(tokens_dir / "tokens.json", json.dumps(record, indent=2) + "\n")
    _write_private(bruno_file, f"accessToken={access_token}\n")


def _say(message: str) -> None:
    sys.stderr.write(message + "\n")


def run(
    env: Mapping[str, str],
    *,
    scope: str = DEFAULT_SCOPE,
    insecure: bool = False,
    http: Callable[..., tuple[int, dict[str, str], bytes]] = _http_request,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    home: Path | None = None,
) -> int:
    """The whole mint; returns the process exit code."""
    try:
        tokens_dir, bruno_file = resolve_isolated_paths(env, home)
    except MintRefused as exc:
        _say(f"refused: {exc}")
        return EXIT_REFUSED

    base = env.get("KEYCLOAK_BASE", DEFAULT_KEYCLOAK_BASE).rstrip("/")
    realm = env.get("KEYCLOAK_REALM", DEFAULT_REALM)
    issuer = f"{base}/realms/{realm}"
    device_url = f"{issuer}/protocol/openid-connect/auth/device"
    token_url = f"{issuer}/protocol/openid-connect/token"

    status, body = _form_post(
        device_url,
        {"client_id": CLIENT_ID, "scope": scope},
        insecure=insecure,
        http=http,
    )
    parsed = _parse(body)
    device_code = parsed.get("device_code") if status == 200 else None
    if not isinstance(device_code, str) or not device_code:
        _say(f"device authorization failed: {json.dumps(_error_summary(status, body))}")
        return EXIT_DEVICE_ERROR

    interval = _positive_int(parsed.get("interval"), 5)
    expires_in = _positive_int(parsed.get("expires_in"), 120)
    _say(f"user_code: {parsed.get('user_code')}")
    _say(f"verification_uri_complete: {parsed.get('verification_uri_complete')}")

    deadline = clock() + expires_in
    while clock() < deadline:
        sleep(interval)
        status, body = _form_post(
            token_url,
            {"grant_type": _GRANT, "client_id": CLIENT_ID, "device_code": device_code},
            insecure=insecure,
            http=http,
        )
        result = _parse(body)
        error = result.get("error")
        if status == 200 and not error:
            return _finish(result, tokens_dir, bruno_file, issuer, clock())
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            _say("denied: the user denied the authorization request")
            return EXIT_DENIED
        if error == "expired_token":
            _say("expired: the device code expired before approval")
            return EXIT_EXPIRED
        _say(f"token request failed: {json.dumps(_error_summary(status, body))}")
        return EXIT_DEVICE_ERROR
    _say("expired: polling window elapsed before approval")
    return EXIT_EXPIRED


def _finish(
    result: Mapping[str, object],
    tokens_dir: Path,
    bruno_file: Path,
    issuer: str,
    now: float,
) -> int:
    access_token = result.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        _say("token request failed: success response carried no access token")
        return EXIT_DEVICE_ERROR
    try:
        persist(
            tokens_dir=tokens_dir,
            bruno_file=bruno_file,
            access_token=access_token,
            expires_in=_positive_int(result.get("expires_in"), 0),
            realm_issuer=issuer,
            now=now,
        )
    except OSError as exc:
        # The exception text can carry a path but never the token.
        _say(f"write failed: {type(exc).__name__}")
        return EXIT_WRITE_FAILED
    summary = claims_summary(decode_claims(access_token))
    _say(f"claims: {json.dumps(summary, sort_keys=True)}")
    _say(f"refresh-token key absent: {'refresh_token' not in result}")
    _say("minted: access token stored in the isolated directory (mode 0600)")
    return EXIT_OK


def _parse(body: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--scope",
        default=DEFAULT_SCOPE,
        help="explicit scope string (default: the two ACL scopes + openid)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification (laptop self-signed front door only)",
    )
    args = parser.parse_args(argv)
    return run(os.environ, scope=args.scope, insecure=args.insecure)


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
