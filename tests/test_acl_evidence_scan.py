"""AC-18 seal scan (``scripts/deploy/acl_evidence_scan.py``) — Addendum E.

The scanner must catch a secret in EVERY form, not one spelling. The
review of 201b74a planted three folders against the first version: a
Keycloak device-endpoint JSON body, a JSON-quoted ``refresh_token`` with an
opaque value plus an ``Authorization: Bearer`` line — both scanned CLEAN —
and the unquoted ``key=value`` form plus a JWT, which were caught.

Tests are parametrised over every form x every key, plus the reviewer's
three exact planted cases. Each secret case is RED (the scan returns a
hit); the negative cases (bare key names, empty values, a 32-hex
``trace_id``, a ``user_code``) are GREEN. Neuter: drop the JSON form from
the key pattern -> the JSON cells go RED (build record).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from scripts.deploy import acl_evidence_scan as scan

OPAQUE = "Zk3Qm9xVb7LpR2sTn8YdW4"  # an opaque (non-JWT) secret value


def _seg(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _jwt() -> str:
    return f"{_seg({'alg': 'RS256'})}.{_seg({'sub': 'x' * 20})}.{'s' * 30}"


KEYS = [
    "access_token",
    "refresh_token",
    "id_token",
    "device_code",
    "client_secret",
    "accessToken",
    "refreshToken",
    "password",
]

# Every form from Addendum E rule 1, with K = key, V = value.
FORMS = {
    "json-compact": '{"K":"V"}',
    "json-whitespace": '{ "K" : "V" }',
    "json-newline-pretty": '{\n  "K" : "V",\n  "x": 1\n}',
    "json-nonstring": '{"K": 123456789}',
    "json-single-quoted": "{'K': 'V'}",
    # Addendum E clarification N2: ANY RFC 8259 whitespace, JSON escaped
    # inside a string (a response body embedded in a log line).
    "json-newline-after-colon": '{"K":\n    "V"}',
    "json-crlf-after-colon": '{"K":\r\n"V"}',
    "json-tab-after-colon": '{"K":\t"V"}',
    "json-newline-before-colon": '{"K"\n  : "V"}',
    "json-nonstring-newline": '{"K":\n  123456789}',
    "json-escaped-in-string": '{\\"K\\":\\"V\\"}',
    "json-escaped-in-log-line": 'INFO body="{\\"K\\": \\"V\\", \\"x\\": 1}"',
    "json-escaped-newline": '{\\"K\\":\n\\"V\\"}',
    "yaml": "K: V",
    "yaml-indented": "creds:\n  K: V\n",
    "env": "K=V",
    "env-export": "export K=V",
    "ini-spaced": "K = V",
    "url-query": "https://kc.example/cb?K=V&state=1",
    "form-body": "grant_type=x&K=V&client_id=y",
}


def _hits(tmp_path: Path, text: str) -> list[Path]:
    (tmp_path / "evidence.txt").write_text(text, encoding="utf-8")
    return scan.scan(tmp_path)


class TestEveryFormTimesEveryKeyIsCaught:
    @pytest.mark.parametrize("form", sorted(FORMS))
    @pytest.mark.parametrize("key", KEYS)
    def test_secret_with_a_value_is_a_hit(
        self, tmp_path: Path, form: str, key: str
    ) -> None:
        text = FORMS[form].replace("K", key).replace("V", OPAQUE)
        assert [p.name for p in _hits(tmp_path, text)] == ["evidence.txt"], (form, key)

    @pytest.mark.parametrize("key", KEYS)
    def test_key_names_match_case_insensitively(self, tmp_path: Path, key: str) -> None:
        assert _hits(tmp_path, f'{{"{key.upper()}": "{OPAQUE}"}}')


class TestBearerAndJwt:
    @pytest.mark.parametrize(
        "line",
        [
            f"Authorization: Bearer {OPAQUE}",
            f"authorization:bearer {OPAQUE}",
            "Authorization: Basic dXNlcjpwYXNz",
            "Authorization: Bearer x",  # any value after the header is a finding
            f"curl -H 'Authorization: Bearer {OPAQUE}' https://x",
            f"the token was Bearer {OPAQUE} in the log",
        ],
    )
    def test_bearer_credentials_are_hits(self, tmp_path: Path, line: str) -> None:
        assert _hits(tmp_path, line)

    @pytest.mark.parametrize(
        "line",
        [
            f"bearer {OPAQUE}",
            f"BEARER {OPAQUE}",
            f"BeArEr {OPAQUE}",
            f"token was bearer {OPAQUE} in the log",
            f"authorization: bearer {OPAQUE}",
            "AUTHORIZATION: BASIC dXNlcjpwYXNz",
        ],
    )
    def test_bearer_scheme_is_case_insensitive(self, tmp_path: Path, line: str) -> None:
        assert _hits(tmp_path, line)

    def test_jwt_shape_is_a_hit(self, tmp_path: Path) -> None:
        assert _hits(tmp_path, f"x {_jwt()} y")


class TestTheReviewersThreePlantedCases:
    def test_keycloak_device_endpoint_json_body(self, tmp_path: Path) -> None:
        body = {
            "device_code": OPAQUE,
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://kc.example/device",
            "expires_in": 120,
            "interval": 5,
        }
        assert _hits(tmp_path, json.dumps(body))
        assert _hits(tmp_path, json.dumps(body, indent=2))

    def test_json_quoted_refresh_token_plus_authorization_bearer(
        self, tmp_path: Path
    ) -> None:
        text = f'{{"refresh_token": "{OPAQUE}"}}\nAuthorization: Bearer {OPAQUE}\n'
        assert _hits(tmp_path, text)

    def test_unquoted_key_value_and_a_jwt(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text(f"refresh_token={OPAQUE}")
        (tmp_path / "b.txt").write_text(_jwt())
        assert [p.name for p in scan.scan(tmp_path)] == ["a.txt", "b.txt"]


class TestClarificationNegativesStayGreen:
    """The N2 widening (newline after the colon, escaped JSON, lowercase
    bearer) must not turn prose or empty values into findings."""

    @pytest.mark.parametrize(
        "text",
        [
            '{"device_code":\n  ""}',
            '{\\"device_code\\":\\"\\"}',
            '{"refresh_token":\r\n}',
            "password:\nthe next line is prose, not a value",
            "refresh_token:\n\nsomething else entirely",
            "a bearer credential",
            "bearer short",
            "the bearer-equivalent device code is never printed",
        ],
    )
    def test_not_a_finding(self, tmp_path: Path, text: str) -> None:
        assert _hits(tmp_path, text) == [], text


class TestNegativesAreGreen:
    @pytest.mark.parametrize(
        "text",
        [
            "refresh-token key absent: True",
            "the device_code is never printed",
            "a bare mention of refresh_token and client_secret without a value",
            "password",
            'password reset flow, "password" mentioned in prose',
            '{"device_code": ""}',
            '{"refresh_token": ""}',
            "device_code=",
            "refresh_token:\n",
            "trace_id 0123456789abcdef0123456789abcdef",
            "user_code: ABCD-EFGH",
            "verification_uri_complete: https://kc.example/device?user_code=ABCD-EFGH",
            "a bearer credential is bearer-equivalent",
            "Bearer short",
            "id_token_hint is a parameter name",
            "authorization is required",
        ],
    )
    def test_not_a_finding(self, tmp_path: Path, text: str) -> None:
        assert _hits(tmp_path, text) == [], text


class TestFailClosed:
    def test_nested_files_scanned(self, tmp_path: Path) -> None:
        (tmp_path / "d" / "e").mkdir(parents=True)
        (tmp_path / "d" / "e" / "x").write_text(f'{{"id_token": "{OPAQUE}"}}')
        assert len(scan.scan(tmp_path)) == 1

    def test_binary_file_is_a_hit(self, tmp_path: Path) -> None:
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02 harmless")
        assert [p.name for p in scan.scan(tmp_path)] == ["blob.bin"]

    def test_unreadable_file_is_a_hit(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "u"
        f.write_text("x")
        real = Path.read_bytes

        def deny(self: Path) -> bytes:
            if self == f:
                raise PermissionError
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", deny)
        assert scan.scan(tmp_path) == [f]

    def test_not_a_directory_fails_closed(self, tmp_path: Path) -> None:
        assert scan.main([str(tmp_path / "missing")]) == 2


class TestMain:
    def test_clean_folder_exit_0(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "a.md").write_text("refresh-token key absent: True\n")
        assert scan.main([str(tmp_path)]) == 0
        assert "clean" in capsys.readouterr().out

    def test_hit_reports_names_never_content(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "leak.json").write_text(f'{{"device_code": "{OPAQUE}"}}')
        assert scan.main([str(tmp_path)]) == 1
        out = capsys.readouterr().out
        assert "leak.json" in out and OPAQUE not in out
