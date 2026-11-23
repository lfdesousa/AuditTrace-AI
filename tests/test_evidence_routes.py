"""CONSOLE-AUDIT-EVIDENCE WU-A -- ``GET /evidence/messages/{id}`` and
``GET /evidence/interactions/{id}`` (contract ``audittrace.evidence.v1``, fixtures v3).

Runs on the container's aiosqlite factory, where NO row-level security exists: what is
proven here is the service-layer ``user_id`` filter, the scope gate, the 404 equality and
the contract. Cross-user isolation under the real policy is
``test_evidence_rls_postgres.py`` (``make test-rls-local``).

Guards: G5 list filter composes; G6 scope; G7 other-user == nonexistent; AC3; AC11' (the
row is produced through the real persist path); the keyset of the response equals the
fixture's (A1 S5); newest row wins and ``lookup.candidates`` counts.

Neuters (each alone): G5 ignore the ``client_message_id`` param -> ``test_list_filter_*``
RED; G6 change the scope list -> ``test_evidence_requires_audit_scope`` RED; G7 answer 403
for another user's row, or drop the service-layer ``user_id`` filter ->
``test_evidence_other_user_equals_nonexistent`` RED; AC11' serve the request's raw
``image_url`` part under ``interaction.input_media`` ->
``test_image_turn_evidence_never_carries_the_bytes`` RED; map NULL -> [] in sources ->
``test_sources_status_grid`` RED.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from audittrace import config
from tests._evidence_rig import (
    AUDIT_SCOPE,
    FIXTURE_V2,
    FIXTURE_V3,
    FIXTURE_V3_IMAGE,
    all_rows,
    get,
    key_paths,
    load,
    row_from_fixture,
    seed,
    tool_call_from_fixture,
)

V2 = load(FIXTURE_V2)
V3 = load(FIXTURE_V3)
V3_IMAGE = load(FIXTURE_V3_IMAGE)
USER = V3["interaction"]["user_id"]
OTHER = "00000000-0000-4000-8000-0000000000ff"
MID = V3["interaction"]["client_message_id"]
READERS_ENV = "AUDITTRACE_EVIDENCE_SERVICE_READERS"


@pytest.fixture
def summariser_readers(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv(READERS_ENV, "audittrace_summariser")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


async def _seed_v3() -> None:
    await seed(row_from_fixture(V3), tool_call_from_fixture(V3, 1, USER))


class TestContract:
    async def test_keyset_equals_fixture_v3(
        self, client: Any, summariser_readers: Any
    ) -> None:
        await _seed_v3()
        r = get(client, f"/evidence/messages/{MID}", USER)
        assert r.status_code == 200
        assert key_paths(r.json()) == key_paths(V3)

    async def test_values_equal_fixture_v3(
        self, client: Any, summariser_readers: Any
    ) -> None:
        await _seed_v3()
        body = get(client, f"/evidence/messages/{MID}", USER).json()
        # created_at / started_at are DB-dialect formatted (SQLite drops the zone and the
        # zero microseconds); every other value is the fixture's.
        expected = copy.deepcopy(V3)  # never mutate the module-level fixture
        for doc in (body, expected):
            doc["interaction"].pop("created_at")
            doc["tool_calls"][0].pop("started_at")
        assert body == expected

    async def test_keyset_and_values_equal_fixture_v3_image(
        self, client: Any, summariser_readers: Any
    ) -> None:
        await seed(row_from_fixture(V3_IMAGE))
        r = get(client, f"/evidence/messages/{MID}", USER)
        assert r.status_code == 200
        body = r.json()
        # no tool call is seeded for the image fixture: its list subtrees are empty here
        expected = {
            p
            for p in key_paths(V3_IMAGE)
            if not p.startswith(("tool_calls[]", "sources.items[]"))
        }
        assert key_paths(body) == expected
        assert (
            body["interaction"]["input_media"] == V3_IMAGE["interaction"]["input_media"]
        )
        assert body["integrity"] == V3_IMAGE["integrity"]
        assert body["integrity"]["recomputed"] == "match"

    async def test_by_interaction_id_is_the_same_document_with_lookup_by_interaction_id(
        self, client: Any, summariser_readers: Any
    ) -> None:
        await _seed_v3()
        by_message = get(client, f"/evidence/messages/{MID}", USER).json()
        by_id = get(client, "/evidence/interactions/1", USER).json()
        assert by_id["lookup"] == {
            "by": "interaction_id",
            "value": "1",
            "candidates": 1,
        }
        assert by_message["lookup"]["by"] == "client_message_id"
        by_message.pop("lookup")
        by_id.pop("lookup")
        assert by_id == by_message

    async def test_the_contract_name(self, client: Any) -> None:
        await _seed_v3()
        assert get(client, f"/evidence/messages/{MID}", USER).json()["contract"] == (
            "audittrace.evidence.v1"
        )


class TestLookup:
    async def test_newest_row_wins_and_candidates_counts(self, client: Any) -> None:
        old = row_from_fixture(V3, id=5, answer="old")
        new = row_from_fixture(V3, id=9, answer="new")
        await seed(old, new)
        body = get(client, f"/evidence/messages/{MID}", USER).json()
        assert body["lookup"]["candidates"] == 2
        assert body["interaction"]["id"] == 9
        assert body["interaction"]["answer"] == "new"

    async def test_candidates_counts_only_the_callers_rows(self, client: Any) -> None:
        await seed(
            row_from_fixture(V3, id=5),
            row_from_fixture(V3, id=6, user_id=OTHER),
        )
        body = get(client, f"/evidence/messages/{MID}", USER).json()
        assert body["lookup"]["candidates"] == 1
        assert body["interaction"]["id"] == 5


def _not_found(r: Any) -> tuple[int, str]:
    return r.status_code, r.text


class TestG7OtherUserEqualsNonexistent:
    async def test_evidence_other_user_equals_nonexistent(self, client: Any) -> None:
        await seed(row_from_fixture(V3, user_id=OTHER))
        theirs = get(client, f"/evidence/messages/{MID}", USER)
        nothing = get(client, "/evidence/messages/no-such-message", USER)
        assert _not_found(theirs) == _not_found(nothing)
        assert theirs.status_code == 404
        assert OTHER not in theirs.text and "fixture" not in theirs.text

    async def test_by_interaction_other_user_equals_nonexistent(
        self, client: Any
    ) -> None:
        await seed(row_from_fixture(V3, user_id=OTHER))
        theirs = get(client, "/evidence/interactions/1", USER)
        nothing = get(client, "/evidence/interactions/424242", USER)
        assert _not_found(theirs) == _not_found(nothing)
        assert theirs.status_code == 404

    async def test_the_owner_still_reads_it(self, client: Any) -> None:
        await seed(row_from_fixture(V3, user_id=OTHER))
        assert get(client, f"/evidence/messages/{MID}", OTHER).status_code == 200

    @pytest.mark.parametrize(
        "bad", ["..", ".", "a%2Fb", "abc%0A", "x" * 256, "%7B%7Bm%7D%7D"]
    )
    async def test_an_invalid_id_is_the_same_404(self, client: Any, bad: str) -> None:
        await seed(row_from_fixture(V3))
        nothing = get(client, "/evidence/messages/no-such-message", USER)
        r = get(client, f"/evidence/messages/{bad}", USER)
        assert _not_found(r) == _not_found(nothing)


#: F1: every id that cannot be a row key answers the SAME 404 as an unknown id: no 500
#: (``interactions.id`` is a 32-bit integer; PostgreSQL's driver refuses to bind more) and
#: no 422. ``2147483647`` is the largest possible key and goes to the database.
MALFORMED_INTERACTION_IDS = [
    "2147483648",
    "99999999999999999999",
    "abc",
    "1.5",
    "0",
    "-1",
    "%D9%A3",  # an Arabic-Indic digit three: int() would accept it
    "1e3",
]


class TestF1MalformedInteractionId:
    @pytest.mark.parametrize("bad", MALFORMED_INTERACTION_IDS)
    async def test_malformed_interaction_id_is_the_same_404(
        self, client: Any, bad: str
    ) -> None:
        await seed(row_from_fixture(V3, user_id=OTHER))
        nothing = get(client, "/evidence/interactions/424242", USER)
        theirs = get(client, "/evidence/interactions/1", USER)
        r = get(client, f"/evidence/interactions/{bad}", USER)
        assert _not_found(r) == _not_found(nothing) == _not_found(theirs)
        assert r.status_code == 404

    async def test_the_largest_key_still_reaches_the_lookup(self, client: Any) -> None:
        await seed(row_from_fixture(V3, id=2147483647))
        r = get(client, "/evidence/interactions/2147483647", USER)
        assert r.status_code == 200
        assert r.json()["interaction"]["id"] == 2147483647

    @pytest.mark.parametrize("bad", MALFORMED_INTERACTION_IDS)
    async def test_a_malformed_interaction_id_never_reaches_the_lookup(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        from unittest.mock import AsyncMock

        spy = AsyncMock(return_value={})
        monkeypatch.setattr("audittrace.routes.evidence._document", spy)
        assert get(client, f"/evidence/interactions/{bad}", USER).status_code == 404
        spy.assert_not_called()

    @pytest.mark.parametrize(
        "bad", ["..", "a%2Fb", "abc%0A", "x" * 256, "%7B%7Bm%7D%7D"]
    )
    async def test_a_malformed_message_id_never_reaches_the_lookup(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        """A1: the shape check is observable. ``a%2Fb`` never matches the route and the
        dot segment collapses in the URL; the rest reach the handler and must stop there."""
        from unittest.mock import AsyncMock

        spy = AsyncMock(return_value={})
        monkeypatch.setattr("audittrace.routes.evidence._document", spy)
        assert get(client, f"/evidence/messages/{bad}", USER).status_code == 404
        spy.assert_not_called()

    async def test_a_valid_message_id_does_reach_the_lookup(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The positive control of the two tests above."""
        from unittest.mock import AsyncMock

        spy = AsyncMock(return_value={"contract": "x"})
        monkeypatch.setattr("audittrace.routes.evidence._document", spy)
        assert get(client, f"/evidence/messages/{MID}", USER).status_code == 200
        spy.assert_called_once()

    @pytest.mark.parametrize(
        "bad", ["2147483648", "99999999999999999999", "-2147483649"]
    )
    async def test_tool_calls_out_of_range_id_is_the_unknown_id_answer(
        self, client: Any, bad: str
    ) -> None:
        """The existing ``/interactions/{id}/tool-calls`` had the same driver error on
        PostgreSQL; out of range now answers as an unknown id does (an empty list)."""
        unknown = get(client, "/interactions/424242/tool-calls", USER)
        r = get(client, f"/interactions/{bad}/tool-calls", USER)
        assert r.status_code == 200
        assert r.json()["tool_calls"] == [] and r.json()["total"] == 0
        assert {k: v for k, v in r.json().items() if k != "interaction_id"} == {
            k: v for k, v in unknown.json().items() if k != "interaction_id"
        }

    async def test_tool_calls_non_integer_keeps_its_422(self, client: Any) -> None:
        assert get(client, "/interactions/abc/tool-calls", USER).status_code == 422


class TestFixtureV2:
    async def test_a_fixture_v2_row_is_a_match(
        self, client: Any, summariser_readers: Any
    ) -> None:
        """A3: the superseded v2 contract (no input_media) is still served as ``match``."""
        await seed(row_from_fixture(V2), tool_call_from_fixture(V2, 1, USER))
        body = get(client, f"/evidence/messages/{MID}", USER).json()
        assert body["integrity"]["recomputed"] == "match"
        assert body["integrity"]["content_hash"] == V2["integrity"]["content_hash"]
        assert body["integrity"]["hashed_fields"] == V2["integrity"]["hashed_fields"]


class TestG6Scope:
    @pytest.mark.parametrize(
        "path", [f"/evidence/messages/{MID}", "/evidence/interactions/1"]
    )
    @pytest.mark.parametrize(
        "scope",
        ["audittrace:query", "", "audittrace:context memory:episodic:read"],
        ids=["query-only", "empty", "other"],
    )
    async def test_evidence_requires_audit_scope(
        self, client: Any, path: str, scope: str
    ) -> None:
        await _seed_v3()
        r = get(client, path, USER, scope)
        assert r.status_code == 403
        assert "fixture" not in r.text

    @pytest.mark.parametrize(
        "path", [f"/evidence/messages/{MID}", "/evidence/interactions/1"]
    )
    async def test_audit_scope_opens_it(self, client: Any, path: str) -> None:
        await _seed_v3()
        assert get(client, path, USER, AUDIT_SCOPE).status_code == 200


class TestG5ListFilter:
    async def test_list_filter_client_message_id(self, client: Any) -> None:
        await seed(
            row_from_fixture(V3, id=1),
            row_from_fixture(V3, id=2, client_message_id="other-message"),
        )
        body = get(client, f"/interactions?client_message_id={MID}", USER).json()
        assert [i["id"] for i in body["interactions"]] == [1]
        assert body["total"] == 1
        assert body["interactions"][0]["client_message_id"] == MID

    async def test_list_filter_composes_with_the_others(self, client: Any) -> None:
        await seed(
            row_from_fixture(V3, id=1),
            row_from_fixture(V3, id=2, session_id="s-2"),
        )
        both = get(
            client,
            f"/interactions?client_message_id={MID}&session_id=s-2",
            USER,
        ).json()
        assert [i["id"] for i in both["interactions"]] == [2]
        none = get(
            client, f"/interactions?client_message_id={MID}&status=failed", USER
        ).json()
        assert none["interactions"] == []

    async def test_an_invalid_filter_value_is_a_422(self, client: Any) -> None:
        r = get(client, "/interactions?client_message_id=..", USER)
        assert r.status_code == 422

    async def test_every_list_item_carries_the_key(self, client: Any) -> None:
        await seed(row_from_fixture(V3, client_message_id=None))
        item = get(client, "/interactions", USER).json()["interactions"][0]
        assert "client_message_id" in item and item["client_message_id"] is None


class TestG8Tamper:
    async def test_integrity_mismatch_after_tamper(self, client: Any) -> None:
        await seed(row_from_fixture(V3, answer="altered after the hash"))
        body = get(client, f"/evidence/messages/{MID}", USER).json()
        assert body["integrity"]["recomputed"] == "mismatch"
        assert body["integrity"]["content_hash"] == V3["integrity"]["content_hash"]

    async def test_an_untouched_row_is_a_match(self, client: Any) -> None:
        await seed(row_from_fixture(V3))
        body = get(client, f"/evidence/messages/{MID}", USER).json()
        assert body["integrity"]["recomputed"] == "match"

    @pytest.mark.parametrize(
        "over",
        [
            {"event_class": "memory_access", "source": "memory-audit"},
            {"event_class": "security"},
        ],
        ids=["memory-audit", "scan-security"],
    )
    async def test_null_hash_rows_are_unavailable(
        self, client: Any, over: dict[str, Any]
    ) -> None:
        await seed(row_from_fixture(V3, content_hash=None, **over))
        body = get(client, "/evidence/interactions/1", USER).json()
        assert body["integrity"]["recomputed"] == "unavailable"
        assert body["integrity"]["content_hash"] is None
        assert body["integrity"]["hashed_fields"]  # emitted as usual
        assert body["interaction"]["source"] == over.get("source", "librechat")

    async def test_the_route_never_stores_a_verdict(self, client: Any) -> None:
        await seed(row_from_fixture(V3))
        get(client, f"/evidence/messages/{MID}", USER)
        rows = await all_rows()
        assert [r.content_hash for r in rows] == [V3["integrity"]["content_hash"]]


class TestSourcesStatus:
    """S9: ``null`` = not captured, ``[]`` = captured empty, a list = captured."""

    @pytest.mark.parametrize(
        ("stored", "flag", "status", "items"),
        [
            (None, None, "not_recorded", 0),
            ([], False, "recorded", 0),
            ([V3["tool_calls"][0]["recall_matches"][0]], False, "recorded", 1),
        ],
        ids=["null", "empty-list", "one-match"],
    )
    async def test_sources_status_grid(
        self, client: Any, stored: Any, flag: Any, status: str, items: int
    ) -> None:
        tc = tool_call_from_fixture(V3, 1, USER)
        tc.recall_matches = stored
        tc.recall_matches_truncated = flag
        await seed(row_from_fixture(V3), tc)
        body = get(client, f"/evidence/messages/{MID}", USER).json()
        assert body["sources"]["status"] == status
        assert len(body["sources"]["items"]) == items
        assert body["tool_calls"][0]["recall_matches"] == stored

    async def test_a_row_with_no_tool_calls_is_not_recorded(self, client: Any) -> None:
        await seed(row_from_fixture(V3))
        body = get(client, f"/evidence/messages/{MID}", USER).json()
        assert body["tool_calls"] == []
        assert body["sources"] == {"status": "not_recorded", "items": []}

    async def test_another_users_tool_call_is_not_served(self, client: Any) -> None:
        await seed(row_from_fixture(V3), tool_call_from_fixture(V3, 1, OTHER))
        body = get(client, f"/evidence/messages/{MID}", USER).json()
        assert body["tool_calls"] == []


class TestVisibility:
    async def test_default_is_operator_managed_unverified(self, client: Any) -> None:
        await seed(row_from_fixture(V3))
        answer = get(client, f"/evidence/messages/{MID}", USER).json()["visibility"][
            "answer"
        ]
        assert answer["api_readers"] == ["owner"]
        assert answer["owner_is_caller"] is True
        assert answer["basis"] == "postgres-rls:tenant_isolation_interactions"
        assert answer["operator_access"] == "database administrators"
        assert answer["service_readers"] == [
            {
                "role": "operator-managed",
                "basis": "unverified",
                "purpose": "session summarisation",
            }
        ]

    async def test_roles_come_from_the_env_value(
        self, client: Any, summariser_readers: Any
    ) -> None:
        await seed(row_from_fixture(V3))
        answer = get(client, f"/evidence/messages/{MID}", USER).json()["visibility"][
            "answer"
        ]
        assert (
            answer["service_readers"] == V3["visibility"]["answer"]["service_readers"]
        )

    async def test_a_blank_env_value_fails_closed_to_operator_managed(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(READERS_ENV, "  ")
        config.get_settings.cache_clear()
        try:
            await seed(row_from_fixture(V3))
            answer = get(client, f"/evidence/messages/{MID}", USER).json()[
                "visibility"
            ]["answer"]
        finally:
            config.get_settings.cache_clear()
        assert answer["service_readers"][0]["role"] == "operator-managed"


class TestAc11ImageTurnThroughTheRealPersistPath:
    @pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
    async def test_image_turn_evidence_never_carries_the_bytes(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, stream: bool
    ) -> None:
        import hashlib

        from tests._image_slice import (
            CANARY,
            PNG_B64,
            flip_mode,
            image_messages,
            ok_fake,
            post_chat,
        )

        for _ in flip_mode(monkeypatch, "inject"):
            response = post_chat(
                client,
                ok_fake("inject", stream),
                stream=stream,
                messages=image_messages(),
            )
        assert response.status_code == 200
        (row,) = await all_rows()
        assert row.input_media is not None
        body = get(client, f"/evidence/interactions/{row.id}", row.user_id or "").json()
        served = body["interaction"]["input_media"]
        assert served == json.loads(row.input_media)
        rendered = json.dumps(served)
        assert CANARY not in rendered and PNG_B64 not in rendered
        assert "data:" not in rendered and "base64" not in rendered
        assert body["integrity"]["recomputed"] == "match"
        # the third-party recipe (stdlib only) reproduces the stored hash from the
        # served object
        interaction = body["interaction"]
        payload = {k: interaction[k] for k in body["integrity"]["hashed_fields"]}
        payload["input_media"] = json.dumps(
            served, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), default=str
            ).encode()
        ).hexdigest()
        assert digest == body["integrity"]["content_hash"]

    async def test_a_text_only_row_serves_null(self, client: Any) -> None:
        await seed(row_from_fixture(V3))
        body = get(client, f"/evidence/messages/{MID}", USER).json()
        assert body["interaction"]["input_media"] is None


class TestRowTypesThroughByInteraction:
    async def test_a_librechat_free_row_has_a_null_message_id_and_looks_up_by_id(
        self, client: Any
    ) -> None:
        await seed(row_from_fixture(V3, client_message_id=None, source="mcp"))
        body = get(client, "/evidence/interactions/1", USER).json()
        assert body["interaction"]["client_message_id"] is None
        assert body["lookup"]["by"] == "interaction_id"
