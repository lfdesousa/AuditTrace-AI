"""``scripts/deploy/acl_e2e_device_mint.py``
(ACL WU-2c-A) — offline, no network, no real token store.

Covers: the isolation refusal (BA-2), the request fields (explicit
``scope=``, the device-code grant), the persisted file shape (no refresh
keys), AC-19 (no token / device code on ANY output path). AC-18's seal scan
has its own module (tests/test_acl_evidence_scan.py). Every guard has an assertion that fails when the guard is removed
(neuters recorded in the build record).
"""

from __future__ import annotations

import base64
import json
import re
import stat
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from scripts.deploy import acl_e2e_device_mint as mint

JWT_RE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")
DEVICE_CODE = "DEVCODE-0123456789-abcdefghijklmnopqrstuvwxyz"
REFRESH = "REFRESHTOK-0123456789-abcdefghijklmnopqrstuvwxyz"
STORE_FILE = (
    "tokens" + ".json"
)  # the persisted file name, built to keep prose greppable


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims: dict) -> str:
    return f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(claims)}.{'s' * 30}"


CLAIMS = {
    "azp": "audittrace-acl-e2e",
    "scope": "openid memory:acl:read-own memory:acl:write",
    "sub": "11111111-1111-1111-1111-111111111111",
    "sid": "22222222-2222-2222-2222-222222222222",
    "iat": 1000,
    "exp": 1900,
}
ACCESS = _jwt(CLAIMS)


class FakeHttp:
    """Scripted transport: one response per call, requests recorded."""

    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, list[str]]]] = []

    def __call__(self, method, url, headers=None, body=None, timeout=30, context=None):
        self.requests.append((url, parse_qs((body or b"").decode())))
        status, payload = self.responses.pop(0)
        return status, {}, payload


def _device_ok() -> tuple[int, bytes]:
    return 200, json.dumps(
        {
            "device_code": DEVICE_CODE,
            "user_code": "ABCD-EFGH",
            "verification_uri_complete": "https://kc.example/device?user_code=ABCD-EFGH",
            "interval": 1,
            "expires_in": 120,
        }
    ).encode()


def _token_ok(**extra) -> tuple[int, bytes]:
    return 200, json.dumps(
        {"access_token": ACCESS, "expires_in": 900, "token_type": "Bearer", **extra}
    ).encode()


def _pending() -> tuple[int, bytes]:
    return 400, json.dumps({"error": "authorization_pending"}).encode()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        mint.TOKENS_DIR_ENV: str(tmp_path / "iso" / "store"),
        mint.BRUNO_ENV_ENV: str(tmp_path / "iso" / "bruno.env"),
        "KEYCLOAK_BASE": "https://kc.example",
    }


def _run(env, http, **kw):
    clock = iter(range(0, 10_000, 1))
    return mint.run(
        env,
        http=http,
        sleep=lambda _s: None,
        clock=lambda: float(next(clock)),
        home=kw.pop("home", Path("/nonexistent-home")),
        **kw,
    )


def _no_leak(capsys, *secrets: str) -> None:
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert not JWT_RE.search(text), "a three-segment token-shaped string was printed"
    for secret in secrets:
        assert secret not in text


class TestIsolationRefusal:
    @pytest.mark.parametrize("missing", [mint.TOKENS_DIR_ENV, mint.BRUNO_ENV_ENV])
    def test_missing_variable_refuses_before_any_network(
        self, env, capsys, missing
    ) -> None:
        del env[missing]
        http = FakeHttp([])
        assert _run(env, http) == mint.EXIT_REFUSED
        assert http.requests == []
        assert missing in capsys.readouterr().err

    def test_tokens_dir_under_default_store_refused(self, env, tmp_path) -> None:
        home = tmp_path / "home"
        env[mint.TOKENS_DIR_ENV] = str(home / ".config" / "audittrace" / "x")
        http = FakeHttp([])
        assert _run(env, http, home=home) == mint.EXIT_REFUSED
        assert http.requests == []

    def test_default_store_itself_refused(self, env, tmp_path) -> None:
        home = tmp_path / "home"
        env[mint.TOKENS_DIR_ENV] = str(home / ".config" / "audittrace")
        assert _run(env, FakeHttp([]), home=home) == mint.EXIT_REFUSED

    def test_bruno_env_under_repo_bruno_refused(self, env) -> None:
        env[mint.BRUNO_ENV_ENV] = str(mint._REPO_ROOT / "bruno" / "audittrace" / ".env")
        http = FakeHttp([])
        assert _run(env, http) == mint.EXIT_REFUSED
        assert http.requests == []

    def test_symlink_into_forbidden_root_is_resolved(self, env, tmp_path) -> None:
        home = tmp_path / "home"
        (home / ".config" / "audittrace").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(home / ".config" / "audittrace")
        env[mint.TOKENS_DIR_ENV] = str(link / "t")
        assert _run(env, FakeHttp([]), home=home) == mint.EXIT_REFUSED

    def test_isolated_paths_accepted(self, env) -> None:
        tokens, bruno = mint.resolve_isolated_paths(env, Path("/nonexistent-home"))
        assert tokens.name == "store" and bruno.name == "bruno.env"

    def test_real_home_default_is_a_forbidden_root(self) -> None:
        roots = mint.forbidden_roots()
        assert (Path.home() / ".config" / "audittrace").resolve() in roots
        assert (mint._REPO_ROOT / "bruno").resolve() in roots


class TestRequestAndPersist:
    def test_device_request_carries_explicit_scope_and_client(self, env) -> None:
        http = FakeHttp([_device_ok(), _pending(), _token_ok()])
        assert _run(env, http) == mint.EXIT_OK
        url, form = http.requests[0]
        assert url.endswith("/realms/audittrace/protocol/openid-connect/auth/device")
        assert form == {
            "client_id": ["audittrace-acl-e2e"],
            "scope": ["openid memory:acl:read-own memory:acl:write"],
        }
        url2, form2 = http.requests[2]
        assert url2.endswith("/protocol/openid-connect/token")
        assert form2["grant_type"] == ["urn:ietf:params:oauth:grant-type:device_code"]
        assert form2["device_code"] == [DEVICE_CODE]

    def test_scope_override_is_sent_verbatim(self, env) -> None:
        http = FakeHttp([_device_ok(), _token_ok()])
        assert _run(env, http, scope="openid audittrace:query") == mint.EXIT_OK
        assert http.requests[0][1]["scope"] == ["openid audittrace:query"]

    def test_file_shape_has_no_refresh_keys_and_is_private(self, env) -> None:
        http = FakeHttp([_device_ok(), _token_ok(refresh_token=REFRESH)])
        assert _run(env, http) == mint.EXIT_OK
        store = Path(env[mint.TOKENS_DIR_ENV]) / STORE_FILE
        record = json.loads(store.read_text())
        assert set(record) == {
            "access_token",
            "access_expires_at",
            "realm_issuer",
            "client_id",
        }
        assert record["client_id"] == "audittrace-acl-e2e"
        assert REFRESH not in store.read_text()
        bruno = Path(env[mint.BRUNO_ENV_ENV])
        assert bruno.read_text() == f"accessToken={ACCESS}\n"
        for path in (store, bruno):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.parent.stat().st_mode) == 0o700

    def test_claims_record_is_non_secret_and_complete(self, env, capsys) -> None:
        http = FakeHttp([_device_ok(), _token_ok()])
        assert _run(env, http) == mint.EXIT_OK
        err = capsys.readouterr().err
        assert "user_code: ABCD-EFGH" in err
        assert "verification_uri_complete: https://kc.example/device" in err
        claims_line = next(ln for ln in err.splitlines() if ln.startswith("claims: "))
        summary = json.loads(claims_line[len("claims: ") :])
        assert summary["azp"] == "audittrace-acl-e2e"
        assert summary["sid"] == CLAIMS["sid"]
        assert summary["exp_minus_iat"] == 900
        assert "refresh-token key absent: True" in err

    def test_refresh_token_presence_is_reported_not_stored(self, env, capsys) -> None:
        http = FakeHttp([_device_ok(), _token_ok(refresh_token=REFRESH)])
        assert _run(env, http) == mint.EXIT_OK
        assert "refresh-token key absent: False" in capsys.readouterr().err

    def test_slow_down_bumps_interval_then_succeeds(self, env) -> None:
        slow = (400, json.dumps({"error": "slow_down"}).encode())
        http = FakeHttp([_device_ok(), slow, _token_ok()])
        assert _run(env, http) == mint.EXIT_OK

    def test_decode_claims_garbage_is_empty(self) -> None:
        assert mint.decode_claims("not-a-jwt") == {}
        assert mint.decode_claims("a.%%%.c") == {}
        assert mint.claims_summary({})["exp_minus_iat"] is None

    def test_decode_claims_non_object_payload_is_empty(self) -> None:
        payload = base64.urlsafe_b64encode(b"[1,2]").decode().rstrip("=")
        assert mint.decode_claims(f"a.{payload}.c") == {}


class TestNoSecretOnAnyOutputPath:
    """AC-19 — every path: success, device-endpoint error, token-endpoint
    error body, poll timeout, write failure. Neuter: echo the raw
    token-endpoint body -> RED (recorded in the build record)."""

    def test_success_path(self, env, capsys) -> None:
        http = FakeHttp([_device_ok(), _token_ok(refresh_token=REFRESH)])
        assert _run(env, http) == mint.EXIT_OK
        _no_leak(capsys, ACCESS, DEVICE_CODE, REFRESH)

    def test_device_endpoint_error_body_is_reduced(self, env, capsys) -> None:
        body = json.dumps(
            {
                "error": "invalid_scope",
                "error_description": "Invalid scopes: audittrace:query",
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "device_code": DEVICE_CODE,
            }
        ).encode()
        assert _run(env, FakeHttp([(400, body)])) == mint.EXIT_DEVICE_ERROR
        captured = capsys.readouterr()
        assert '"error": "invalid_scope"' in captured.err
        for secret in (ACCESS, REFRESH, DEVICE_CODE):
            assert secret not in captured.out + captured.err

    def test_device_endpoint_non_json_body(self, env, capsys) -> None:
        assert _run(env, FakeHttp([(502, b"<html>bad gateway</html>")])) == (
            mint.EXIT_DEVICE_ERROR
        )
        assert "<html>" not in capsys.readouterr().err

    def test_token_endpoint_error_body_is_reduced(self, env, capsys) -> None:
        body = json.dumps(
            {
                "error": "invalid_grant",
                "error_description": "bad",
                "device_code": DEVICE_CODE,
                "access_token": ACCESS,
            }
        ).encode()
        assert _run(env, FakeHttp([_device_ok(), (400, body)])) == (
            mint.EXIT_DEVICE_ERROR
        )
        _no_leak(capsys, ACCESS, DEVICE_CODE)

    def test_poll_timeout(self, env, capsys) -> None:
        http = FakeHttp([_device_ok()] + [_pending()] * 400)
        assert _run(env, http) == mint.EXIT_EXPIRED
        _no_leak(capsys, ACCESS, DEVICE_CODE)

    def test_denied_and_expired_token_errors(self, env, capsys) -> None:
        denied = (400, json.dumps({"error": "access_denied"}).encode())
        expired = (400, json.dumps({"error": "expired_token"}).encode())
        assert _run(env, FakeHttp([_device_ok(), denied])) == mint.EXIT_DENIED
        assert _run(env, FakeHttp([_device_ok(), expired])) == mint.EXIT_EXPIRED
        _no_leak(capsys, DEVICE_CODE)

    def test_write_failure(self, env, capsys, monkeypatch) -> None:
        def boom(*_a, **_k):
            raise PermissionError(f"cannot write {ACCESS}")

        monkeypatch.setattr(mint, "persist", boom)
        assert _run(env, FakeHttp([_device_ok(), _token_ok()])) == (
            mint.EXIT_WRITE_FAILED
        )
        _no_leak(capsys, ACCESS, DEVICE_CODE)

    def test_success_without_access_token(self, env, capsys) -> None:
        ok_no_token = (200, json.dumps({"expires_in": 5}).encode())
        assert _run(env, FakeHttp([_device_ok(), ok_no_token])) == (
            mint.EXIT_DEVICE_ERROR
        )
        _no_leak(capsys, DEVICE_CODE)


class TestMainEntry:
    def test_main_refuses_without_isolation_env(self, monkeypatch) -> None:
        monkeypatch.delenv(mint.TOKENS_DIR_ENV, raising=False)
        monkeypatch.delenv(mint.BRUNO_ENV_ENV, raising=False)
        assert mint.main([]) == mint.EXIT_REFUSED

    def test_main_passes_scope_and_insecure(self, monkeypatch) -> None:
        seen: dict = {}

        def fake_run(environ, *, scope, insecure):
            seen.update(scope=scope, insecure=insecure)
            return 0

        monkeypatch.setattr(mint, "run", fake_run)
        assert mint.main(["--scope", "openid x", "--insecure"]) == 0
        assert seen == {"scope": "openid x", "insecure": True}
