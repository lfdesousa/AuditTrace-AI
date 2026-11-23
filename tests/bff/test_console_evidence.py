"""CONSOLE-AUDIT-EVIDENCE WU-C (ADDENDUM-A6 section 3, A7 C2/C4): the BFF's
evidence proxy and its dedicated audience, layer by layer (G13'').

Routes: ``GET /console/evidence/messages/{client_message_id}`` and
``GET /console/evidence/interactions/{interaction_id:int}``, nothing else, each
forwarded to the memory server's ``/evidence/...`` with a token exchanged for
exactly ``audittrace:audit`` from the dedicated audience
``audittrace-librechat-evidence``.

One class per layer; the neuter of each layer is run alone (recorded in the
build record, each RED then GREEN on restore):

* (b) pre-check   -- ``TestPreCheck``: drop the ``missing_evidence_scopes`` check.
* (c) ceiling     -- ``TestCeiling``: omit ``scope_ceiling=`` at the route.
* (d) audience    -- ``TestAudienceAndRequest``: pass ``chat_exchange_audience``.
* (e) granted     -- ``TestGrantedLine``: alter the granted field in
  ``bff.exchange._log_scope_outcome``.
* path confinement (G20a/b) -- restore ``{path:path}`` / drop ``fullmatch``.
* relay (G14)     -- translate 401/403/404 to 502.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from jose import jwt
from pydantic import ValidationError

from bff.app import create_app, get_http_client
from bff.config import Settings, get_settings
from bff.console_evidence_scopes import (
    EVIDENCE_SCOPE_CEILING,
    EVIDENCE_SCOPE_STRING,
    EVIDENCE_SCOPES,
)
from tests.bff.conftest import make_token
from tests.bff.test_app import (
    KEYCLOAK_TOKEN_URL,
    ORCHESTRATOR_BASE,
    _settings,
)
from tests.bff.test_chat_exchange_scope import (
    Keycloak24AndOrchestrator,
    login_token_scopes,
    restricted_token_scopes,
)

EVIDENCE_AUDIENCE = "audittrace-librechat-evidence"
CHAT_AUDIENCE = "audittrace-librechat-chat"
MESSAGE_ID = "b7b3f4e0-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
MESSAGE_ROUTE = f"/console/evidence/messages/{MESSAGE_ID}"
INTERACTION_ROUTE = "/console/evidence/interactions/36641"
UPSTREAM_MESSAGE = f"{ORCHESTRATOR_BASE}/evidence/messages/{MESSAGE_ID}"
UPSTREAM_INTERACTION = f"{ORCHESTRATOR_BASE}/evidence/interactions/36641"
EVIDENCE_BODY = b'{"interaction":{"id":36641},"lookup":{"by":"interaction_id"}}'
EVENT = "evidence.caller_scope_insufficient"


class EvidenceBackend(Keycloak24AndOrchestrator):
    """The shared Keycloak 24 model plus the memory server's evidence routes."""

    def __init__(self) -> None:
        super().__init__()
        self.evidence_requests: list[httpx.Request] = []
        self.evidence_answer: httpx.Response | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{ORCHESTRATOR_BASE}/evidence/"):
            self.evidence_requests.append(request)
            if self.evidence_answer is not None:
                return self.evidence_answer
            return httpx.Response(
                200, content=EVIDENCE_BODY, headers={"content-type": "application/json"}
            )
        return super().handler(request)

    def forwarded_evidence_scopes(self) -> list[str]:
        assert len(self.evidence_requests) == 1
        bearer = self.evidence_requests[0].headers["authorization"]
        assert bearer.startswith("Bearer ")
        claims = jwt.get_unverified_claims(bearer.removeprefix("Bearer "))
        return sorted(claims["scope"].split())


@pytest.fixture
def evidence() -> Iterator[tuple[TestClient, EvidenceBackend]]:
    backend = EvidenceBackend()
    app = create_app()
    app.dependency_overrides[get_settings] = _settings
    with contextlib.ExitStack() as stack:
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
        stack.callback(lambda: asyncio.run(upstream.aclose()))
        app.dependency_overrides[get_http_client] = lambda: upstream
        yield stack.enter_context(TestClient(app)), backend


def _bearer(scopes: set[str] | None = None, sub: str = "alice") -> dict[str, str]:
    held = login_token_scopes() if scopes is None else scopes
    token = make_token(sub=sub, extra_claims={"scope": " ".join(sorted(held))})
    return {"Authorization": f"Bearer {token}"}


def _granted_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("token_exchange.scopes granted=")
    ]


class TestScopesModule:
    def test_the_scope_tuple_is_exactly_audit(self) -> None:
        assert EVIDENCE_SCOPES == ("audittrace:audit",)
        assert EVIDENCE_SCOPE_STRING == "audittrace:audit"

    def test_the_ceiling_is_audit_plus_the_protocol_scopes_only(self) -> None:
        assert EVIDENCE_SCOPE_CEILING == frozenset(
            {"audittrace:audit", "offline_access", "openid"}
        )

    def test_the_login_token_really_carries_audit(self) -> None:
        """Positive control: a green 200 below is not a caller with no audit."""
        assert "audittrace:audit" in login_token_scopes()

    def test_the_restricted_client_really_lacks_audit(self) -> None:
        assert "audittrace:audit" not in restricted_token_scopes()


class TestSettings:
    def test_the_default_is_the_dedicated_audience(self) -> None:
        settings = Settings(exchange_client_secret="x")  # noqa: S106
        assert settings.evidence_exchange_audience == EVIDENCE_AUDIENCE

    def test_it_is_neither_the_login_nor_the_chat_audience(self) -> None:
        settings = Settings(exchange_client_secret="x")  # noqa: S106
        assert settings.evidence_exchange_audience not in {
            settings.exchange_audience,
            settings.chat_exchange_audience,
        }

    def test_an_empty_audience_fails_at_startup_no_fallback(self) -> None:
        with pytest.raises(ValidationError):
            Settings(exchange_client_secret="x", evidence_exchange_audience="")  # noqa: S106

    def test_the_orchestrator_prefix_is_evidence(self) -> None:
        settings = Settings(exchange_client_secret="x")  # noqa: S106
        assert settings.orchestrator_evidence_path_prefix == "/evidence"


class TestHappyPath:
    @pytest.mark.parametrize(
        ("route", "upstream"),
        [(MESSAGE_ROUTE, UPSTREAM_MESSAGE), (INTERACTION_ROUTE, UPSTREAM_INTERACTION)],
        ids=["message", "interaction"],
    )
    def test_the_answer_is_relayed_byte_identical(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        route: str,
        upstream: str,
    ) -> None:
        client, backend = evidence
        response = client.get(route, headers=_bearer())
        assert response.status_code == 200
        assert response.content == EVIDENCE_BODY
        assert [str(r.url) for r in backend.evidence_requests] == [upstream]
        assert backend.evidence_requests[0].method == "GET"

    def test_the_upstream_sees_the_minted_token_and_the_source_label_only(
        self, evidence: tuple[TestClient, EvidenceBackend]
    ) -> None:
        client, backend = evidence
        headers = _bearer() | {"X-Evil": "1", "X-Source": "evil", "Cookie": "a=b"}
        client.get(MESSAGE_ROUTE, headers=headers)
        seen = {k.lower(): v for k, v in backend.evidence_requests[0].headers.items()}
        assert seen["x-source"] == "librechat"
        assert seen["authorization"] != headers["Authorization"]
        assert "x-evil" not in seen
        assert "cookie" not in seen

    def test_the_query_string_is_not_forwarded(
        self, evidence: tuple[TestClient, EvidenceBackend]
    ) -> None:
        client, backend = evidence
        client.get(MESSAGE_ROUTE + "?user_id=bob&limit=1", headers=_bearer())
        assert str(backend.evidence_requests[0].url) == UPSTREAM_MESSAGE

    def test_an_id_is_percent_encoded_for_the_upstream(
        self, evidence: tuple[TestClient, EvidenceBackend]
    ) -> None:
        client, backend = evidence
        client.get("/console/evidence/messages/a:b_1.2-3", headers=_bearer())
        assert backend.evidence_requests[0].url.raw_path == (
            b"/evidence/messages/a%3Ab_1.2-3"
        )

    def test_the_forwarded_token_carries_exactly_audit(
        self, evidence: tuple[TestClient, EvidenceBackend]
    ) -> None:
        client, backend = evidence
        assert client.get(MESSAGE_ROUTE, headers=_bearer()).status_code == 200
        assert backend.forwarded_evidence_scopes() == ["audittrace:audit"]


class TestPreCheck:
    """(b) A caller whose own token lacks ``audittrace:audit`` is refused 403
    BEFORE any exchange (RFC 6750 insufficient_scope); Keycloak saw nothing."""

    @pytest.mark.parametrize(
        "held",
        [
            restricted_token_scopes(),
            {"audittrace:query"},
            set(),
            None,
        ],
        ids=["restricted-client", "query-only", "empty-scope", "no-scope-claim"],
    )
    @pytest.mark.parametrize("route", [MESSAGE_ROUTE, INTERACTION_ROUTE])
    def test_403_before_any_exchange(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        caplog: pytest.LogCaptureFixture,
        held: set[str] | None,
        route: str,
    ) -> None:
        client, backend = evidence
        claims = None if held is None else {"scope": " ".join(sorted(held))}
        token = make_token(sub="alice", extra_claims=claims)
        with caplog.at_level(logging.INFO, logger="bff"):
            response = client.get(route, headers={"Authorization": f"Bearer {token}"})
        message = (
            "This sign-in cannot read audit evidence: its access token lacks the "
            "required scope(s) 'audittrace:audit'. Sign out and sign in again. If "
            "this message returns, ask an administrator to grant the scope(s) to "
            "this client."
        )
        assert response.status_code == 403
        assert response.json() == {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": "insufficient_scope",
                "status": 403,
                "missing_scopes": ["audittrace:audit"],
            },
            "detail": message,
        }
        assert response.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="audittrace:audit"'
        )
        assert backend.exchange_forms == []
        assert backend.evidence_requests == []
        assert _granted_lines(caplog) == []
        events = [r.getMessage() for r in caplog.records if EVENT in r.getMessage()]
        assert events == [f"{EVENT} sub=alice missing=audittrace:audit"]

    def test_a_caller_with_audit_is_served(
        self, evidence: tuple[TestClient, EvidenceBackend]
    ) -> None:
        client, backend = evidence
        response = client.get(MESSAGE_ROUTE, headers=_bearer({"audittrace:audit"}))
        assert response.status_code == 200
        assert len(backend.exchange_forms) == 1


class TestCeiling:
    """(c) Keycloak answers 200 with more than the ceiling: 502 and the memory
    server is never called; no ``granted=`` line."""

    @pytest.mark.parametrize(
        "extra",
        ["audittrace:query", "audittrace:admin", "memory:episodic:read"],
    )
    @pytest.mark.parametrize("route", [MESSAGE_ROUTE, INTERACTION_ROUTE])
    def test_an_over_grant_is_a_502_with_zero_upstream_calls(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        caplog: pytest.LogCaptureFixture,
        extra: str,
        route: str,
    ) -> None:
        client, backend = evidence
        backend.grant_override = {"audittrace:audit", extra}
        with caplog.at_level(logging.INFO, logger="bff"):
            response = client.get(route, headers=_bearer())
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "token_exchange_invalid"
        assert backend.evidence_requests == []
        assert f"unexpected={extra}" in caplog.text
        assert _granted_lines(caplog) == []
        assert "scope ceiling exceeded" in caplog.text

    @pytest.mark.parametrize("protocol", ["openid", "offline_access"])
    def test_a_protocol_scope_is_not_an_over_grant(
        self, evidence: tuple[TestClient, EvidenceBackend], protocol: str
    ) -> None:
        client, backend = evidence
        backend.grant_override = {"audittrace:audit", protocol}
        assert client.get(MESSAGE_ROUTE, headers=_bearer()).status_code == 200
        assert len(backend.evidence_requests) == 1

    def test_the_ceiling_is_bounded_by_the_callers_own_scopes(
        self, evidence: tuple[TestClient, EvidenceBackend]
    ) -> None:
        """A protocol scope the CALLER does not hold is still tolerated (it is
        always in the ceiling); ``audittrace:query`` is refused even when the
        caller holds it, because the evidence ceiling does not name it."""
        client, backend = evidence
        backend.grant_override = {"audittrace:audit", "audittrace:query"}
        caller = _bearer({"audittrace:audit", "audittrace:query"})
        assert client.get(MESSAGE_ROUTE, headers=caller).status_code == 502
        assert backend.evidence_requests == []


class TestAudienceAndRequest:
    """(d) The exchange asks the dedicated audience for exactly audit."""

    def test_the_audience_is_the_evidence_client(
        self, evidence: tuple[TestClient, EvidenceBackend]
    ) -> None:
        client, backend = evidence
        client.get(MESSAGE_ROUTE, headers=_bearer())
        assert len(backend.exchange_forms) == 1
        form = backend.exchange_forms[0]
        assert form["audience"] == [EVIDENCE_AUDIENCE]
        assert form["audience"] != [CHAT_AUDIENCE]
        assert form["scope"] == ["audittrace:audit"]

    def test_the_audience_follows_the_setting(
        self, evidence: tuple[TestClient, EvidenceBackend]
    ) -> None:
        client, backend = evidence
        client.app.dependency_overrides[get_settings] = lambda: _settings().model_copy(  # type: ignore[attr-defined]
            update={"evidence_exchange_audience": "some-other-audience"}
        )
        client.get(MESSAGE_ROUTE, headers=_bearer())
        assert backend.exchange_forms[0]["audience"] == ["some-other-audience"]

    def test_the_chat_audience_would_not_mint_audit(self) -> None:
        """Why the audience matters (measured model): the chat audience's
        defaults have no ``audittrace:audit``, so a mint from it is not audit."""
        backend = EvidenceBackend()
        form = {
            "audience": [CHAT_AUDIENCE],
            "scope": ["audittrace:audit"],
            "subject_token": [_bearer()["Authorization"].removeprefix("Bearer ")],
        }
        granted = backend._grant(form)
        assert granted is not None
        assert "audittrace:audit" not in granted

    def test_one_exchange_per_request(
        self, evidence: tuple[TestClient, EvidenceBackend]
    ) -> None:
        client, backend = evidence
        client.get(MESSAGE_ROUTE, headers=_bearer())
        client.get(INTERACTION_ROUTE, headers=_bearer())
        assert len(backend.exchange_forms) == 2


class TestGrantedLine:
    """(e) The ``granted=`` line names exactly ``audittrace:audit`` (plus at most
    the protocol scopes). C4 neuter: alter the granted field -> RED."""

    def test_the_line_names_exactly_audit(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _ = evidence
        with caplog.at_level(logging.INFO, logger="bff.exchange"):
            client.get(MESSAGE_ROUTE, headers=_bearer())
        lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("token_exchange.")
        ]
        assert lines == ["token_exchange.scopes granted=audittrace:audit"]

    def test_protocol_scopes_may_follow_audit(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, backend = evidence
        backend.grant_override = {"audittrace:audit", "offline_access", "openid"}
        with caplog.at_level(logging.INFO, logger="bff.exchange"):
            client.get(MESSAGE_ROUTE, headers=_bearer())
        assert _granted_lines(caplog) == [
            "token_exchange.scopes granted=audittrace:audit,offline_access,openid"
        ]


class TestRelay:
    """G14: 401/403/404 (and any other status) are relayed, never translated."""

    @pytest.mark.parametrize("status", [401, 403, 404, 422, 500])
    @pytest.mark.parametrize("route", [MESSAGE_ROUTE, INTERACTION_ROUTE])
    def test_the_status_and_body_are_unchanged(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        status: int,
        route: str,
    ) -> None:
        client, backend = evidence
        body = b'{"detail":"as the memory server said it"}'
        backend.evidence_answer = httpx.Response(
            status, content=body, headers={"content-type": "application/json"}
        )
        response = client.get(route, headers=_bearer())
        assert response.status_code == status
        assert response.content == body
        assert len(backend.evidence_requests) == 1

    def test_an_unreachable_memory_server_is_502_upstream_unavailable(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, backend = evidence

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        backend.evidence_answer = None
        original = backend.handler

        def handler(request: httpx.Request) -> httpx.Response:
            if "/evidence/" in str(request.url):
                return boom(request)
            return original(request)

        down = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client.app.dependency_overrides[get_http_client] = lambda: down  # type: ignore[attr-defined]
        try:
            response = client.get(MESSAGE_ROUTE, headers=_bearer())
        finally:
            asyncio.run(down.aclose())
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "upstream_unavailable"


class TestInboundToken:
    @pytest.mark.parametrize("route", [MESSAGE_ROUTE, INTERACTION_ROUTE])
    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "Bearer not-a-jwt"}, {"Authorization": "Basic abc"}],
        ids=["missing", "invalid", "wrong-scheme"],
    )
    def test_401_and_nothing_downstream(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        route: str,
        headers: dict[str, str],
    ) -> None:
        client, backend = evidence
        response = client.get(route, headers=headers)
        assert response.status_code == 401
        assert backend.exchange_forms == []
        assert backend.evidence_requests == []


class TestPathConfinement:
    """G20a/b: two explicit GET routes, one shared id predicate (A1 section
    3.2). A bad id is 404 and the mock transport saw 0 calls of any kind."""

    @pytest.mark.parametrize(
        "ident",
        [
            "..",
            ".",
            "%2e%2e",
            "a%2Fb",
            "a%5Cb",
            "a b",
            "a%20b",
            "x" * 256,
            "a;b",
            "caf%C3%A9",
            "..%2F..%2Fsystem",
        ],
    )
    def test_a_bad_message_id_is_404_with_zero_calls(
        self, evidence: tuple[TestClient, EvidenceBackend], ident: str
    ) -> None:
        client, backend = evidence
        response = client.get(f"/console/evidence/messages/{ident}", headers=_bearer())
        assert response.status_code == 404
        assert backend.exchange_forms == []
        assert backend.evidence_requests == []

    @pytest.mark.parametrize("ident", ["...", "a..b", "x" * 255, "msg:1_2.3-4"])
    def test_a_good_message_id_is_served(
        self, evidence: tuple[TestClient, EvidenceBackend], ident: str
    ) -> None:
        client, backend = evidence
        response = client.get(f"/console/evidence/messages/{ident}", headers=_bearer())
        assert response.status_code == 200
        assert len(backend.evidence_requests) == 1

    @pytest.mark.parametrize("ident", ["abc", "-1", "1.5", "1%2F2", "0x10", ".."])
    def test_a_non_integer_interaction_id_is_404_with_zero_calls(
        self, evidence: tuple[TestClient, EvidenceBackend], ident: str
    ) -> None:
        client, backend = evidence
        response = client.get(
            f"/console/evidence/interactions/{ident}", headers=_bearer()
        )
        assert response.status_code == 404
        assert backend.exchange_forms == []
        assert backend.evidence_requests == []

    @pytest.mark.parametrize(
        "path",
        [
            "/console/evidence",
            "/console/evidence/",
            "/console/evidence/other/1",
            "/console/evidence/messages",
            "/console/evidence/messages/a/b",
        ],
    )
    def test_no_other_path_is_served(
        self, evidence: tuple[TestClient, EvidenceBackend], path: str
    ) -> None:
        client, backend = evidence
        assert client.get(path, headers=_bearer()).status_code == 404
        assert backend.exchange_forms == []

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    @pytest.mark.parametrize("route", [MESSAGE_ROUTE, INTERACTION_ROUTE])
    def test_only_get_is_allowed(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        method: str,
        route: str,
    ) -> None:
        client, backend = evidence
        response = client.request(method, route, headers=_bearer())
        assert response.status_code == 405
        assert backend.exchange_forms == []
        assert backend.evidence_requests == []

    def test_the_two_routes_are_the_only_evidence_routes(self) -> None:
        paths = sorted(
            r.path  # type: ignore[attr-defined]
            for r in create_app().routes
            if "evidence" in getattr(r, "path", "")
        )
        assert paths == [
            "/console/evidence/interactions/{interaction_id:int}",
            "/console/evidence/messages/{client_message_id}",
        ]


class TestLastResort:
    def test_an_unforeseen_failure_is_the_fixed_500(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, backend = evidence

        async def boom(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("unforeseen")

        monkeypatch.setattr("bff.app.exchange_token", boom)
        response = client.get(MESSAGE_ROUTE, headers=_bearer())
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert backend.evidence_requests == []


class TestExchangeFailures:
    @pytest.mark.parametrize(
        ("status", "error", "expected_status", "code"),
        [
            (403, "access_denied", 502, "token_exchange_refused"),
            (400, "invalid_token", 401, "invalid_token"),
            (503, "x", 502, "auth_service_unavailable"),
        ],
    )
    def test_keycloak_refusals_map_like_every_other_route(
        self,
        evidence: tuple[TestClient, EvidenceBackend],
        status: int,
        error: str,
        expected_status: int,
        code: str,
    ) -> None:
        client, backend = evidence
        original = backend.handler

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == KEYCLOAK_TOKEN_URL:
                backend.exchange_forms.append(parse_qs(request.read().decode()))
                return httpx.Response(status, json={"error": error})
            return original(request)

        failing = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client.app.dependency_overrides[get_http_client] = lambda: failing  # type: ignore[attr-defined]
        try:
            response = client.get(MESSAGE_ROUTE, headers=_bearer())
        finally:
            asyncio.run(failing.aclose())
        assert response.status_code == expected_status
        assert response.json()["error"]["code"] == code
        assert backend.evidence_requests == []
