"""LC-9 WP1 -- unit tests for ``services/memory_keys.py`` (the owner-in-the-key
derivation and the pure write/read resolver).

Each guard has a neuter that turns the named test RED (the reviewer applies
them one at a time, with ``PYTHONDONTWRITEBYTECODE=1``):

====================================  =====================================  ==========================
guard                                 neuter                                  goes RED
====================================  =====================================  ==========================
owner prefix in ``physical_key``      ``return logical``                      TestPhysicalKey
``private_physical_id`` always        idempotent prefix (skip when already    TestPrivatePhysicalId
prepends                              prefixed)
sub rule ``check_sub``                drop the ``__`` / trailing ``_`` arms   TestCheckSub
length cap ``check_logical``          drop the cap                            TestCheckLogical
``logical_key`` strips only the       strip any ``/`` prefix                  TestLogicalKey
row's own owner
``logical_id`` inverse                return ``physical``                     TestLogicalId
``deleted_key_candidates``            drop the owner-keyed candidate          TestDeletedKeyCandidates
bare key read only through            drop the ``created_by == owner``        TestResolveWrite (legacy),
identity-pinned predicates            term of the legacy arm                  TestResolveRead
corpus write over ANY private row     return ``corpus`` for a private row     TestResolveWrite (corpus)
foreign / tier_conflict at target     treat as ``own``                        TestResolveWrite (target)
one owner-free refusal text           interpolate the owner into the text     TestRefusalText
====================================  =====================================  ==========================
"""

from __future__ import annotations

import pytest

from audittrace.identity import UserContext
from audittrace.services.memory_keys import (
    ID_FORM_LC9,
    LOGICAL_KEY_MAX,
    ChromaIdConflictError,
    CrossOwnerWriteError,
    InvalidPrivateSubjectError,
    LogicalKeyTooLongError,
    ManifestAuthorizationError,
    RowFacts,
    check_logical,
    check_sub,
    deleted_key_candidates,
    logical_id,
    logical_key,
    new_form_owner,
    physical_key,
    private_physical_id,
    private_storage_key,
    resolve_read,
    resolve_write,
    tier_conflict_message,
)


def _user(sub: str) -> UserContext:
    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=(), is_admin=False
    )


def _row(key: str, tier: str, owner: str, deleted_at_ms: int | None = None) -> RowFacts:
    return RowFacts(
        key=key, tier=tier, created_by_user_id=owner, deleted_at_ms=deleted_at_ms
    )


class TestPhysicalKey:
    def test_prefixes_the_owner(self) -> None:
        assert physical_key("user-a", "decisions/x.md") == "user-a/decisions/x.md"

    def test_two_owners_never_share_a_key(self) -> None:
        assert physical_key("user-a", "k") != physical_key("user-b", "k")

    def test_never_equals_the_bare_key(self) -> None:
        assert physical_key("user-a", "k") != "k"

    def test_user_context_form_matches(self) -> None:
        assert private_storage_key(_user("user-a"), "k") == physical_key("user-a", "k")


class TestPrivatePhysicalId:
    def test_prepends_owner_double_underscore(self) -> None:
        assert private_physical_id(_user("user-a"), "doc") == "user-a__doc"

    def test_always_prepends_even_when_already_prefixed(self) -> None:
        """ADDENDUM-1 S5: an idempotent prefix aliases ``foo`` and
        ``user-a__foo``; always-prepend keeps them two documents."""
        user = _user("user-a")
        assert private_physical_id(user, "foo") != private_physical_id(
            user, "user-a__foo"
        )
        assert private_physical_id(user, "user-a__foo") == "user-a__user-a__foo"


class TestCheckSub:
    @pytest.mark.parametrize("sub", ["a_", "a/b", "a__b", "", "x" * 37, "a__"])
    def test_refuses_non_injective_or_oversize_subs(self, sub: str) -> None:
        with pytest.raises(InvalidPrivateSubjectError):
            check_sub(sub)

    @pytest.mark.parametrize(
        "sub", ["user-a", "a_b", "00000000-0000-0000-0000-000000000001", "x" * 36]
    )
    def test_accepts_ordinary_subs(self, sub: str) -> None:
        assert check_sub(sub) == sub

    def test_error_maps_to_403_family_and_names_no_value(self) -> None:
        with pytest.raises(ManifestAuthorizationError) as exc:
            check_sub("a/b")
        assert "a/b" not in str(exc.value)

    def test_prefixes_are_collision_free_over_the_small_alphabet(self) -> None:
        """The injectivity argument, brute-forced over ``a_/`` up to length 4:
        no two accepted subs share a ``{sub}/`` prefix-or-``{sub}__`` prefix
        with a different sub (ADDENDUM-1 S5: 0 collisions)."""
        import itertools

        accepted = []
        for n in range(1, 5):
            for combo in itertools.product("a_/", repeat=n):
                sub = "".join(combo)
                try:
                    accepted.append(check_sub(sub))
                except InvalidPrivateSubjectError:
                    continue
        for first in accepted:
            for second in accepted:
                if first == second:
                    continue
                assert not (first + "/").startswith(second + "/")
                assert not (first + "__x").startswith(second + "__")


class TestCheckLogical:
    def test_cap_is_218(self) -> None:
        assert LOGICAL_KEY_MAX == 218

    def test_218_ok_219_refused(self) -> None:
        assert check_logical("k" * 218) == "k" * 218
        with pytest.raises(LogicalKeyTooLongError):
            check_logical("k" * 219)

    def test_longest_key_with_longest_sub_fits_the_column(self) -> None:
        assert len(physical_key("x" * 36, "k" * 218)) == 255

    def test_219_is_a_value_error_for_the_400_mapping(self) -> None:
        assert issubclass(LogicalKeyTooLongError, ValueError)


class TestLogicalKey:
    def test_strips_the_rows_own_owner_prefix_for_private(self) -> None:
        assert logical_key("a/decisions/x", tier="private", owner="a") == "decisions/x"

    def test_leaves_a_legacy_bare_key_alone(self) -> None:
        assert logical_key("decisions/x", tier="private", owner="a") == "decisions/x"

    def test_never_strips_another_owners_prefix(self) -> None:
        assert (
            logical_key("b/decisions/x", tier="private", owner="a") == "b/decisions/x"
        )

    def test_never_strips_a_corpus_key(self) -> None:
        assert logical_key("a/decisions/x", tier="corpus", owner="a") == "a/decisions/x"

    def test_s3_uri_key_untouched(self) -> None:
        uri = "s3://quarantine/abc"
        assert logical_key(uri, tier="corpus", owner="a") == uri

    def test_prefix_must_include_the_slash(self) -> None:
        assert logical_key("ab/x", tier="private", owner="a") == "ab/x"


class TestLogicalId:
    def test_inverse_of_physical_id(self) -> None:
        assert logical_id(private_physical_id(_user("user-a"), "d"), "user-a") == "d"

    def test_other_owner_untouched(self) -> None:
        assert logical_id("user-b__d", "user-a") == "user-b__d"

    def test_no_owner_untouched(self) -> None:
        assert logical_id("user-a__d", None) == "user-a__d"


class TestDeletedKeyCandidates:
    """ADDENDUM-5 section 4 #5: EXACTLY ONE candidate per row, decided by the
    row's own stamp (``owner`` is ``new_form_owner(meta)``)."""

    def test_new_form_chunk_has_only_the_owner_keyed_candidate(self) -> None:
        assert deleted_key_candidates("decisions", "user-a__d1", "user-a") == {
            "user-a/decisions/d1"
        }

    def test_legacy_chunk_has_only_the_bare_candidate(self) -> None:
        assert deleted_key_candidates("decisions", "d1", None) == {"decisions/d1"}
        assert deleted_key_candidates("decisions", "user-a__d1", None) == {
            "decisions/user-a__d1"
        }


class TestNewFormOwner:
    """ADDENDUM-5 D0: the identity pin. Anything but a private row stamped
    ``lc9`` with a non-empty string owner is LEGACY (``None``)."""

    def test_stamped_private_row_names_its_owner(self) -> None:
        meta = {"tier": "private", "id_form": ID_FORM_LC9, "user_id": "user-a"}
        assert new_form_owner(meta) == "user-a"

    @pytest.mark.parametrize(
        "meta",
        [
            None,
            {},
            {"tier": "private", "user_id": "u"},
            {"tier": "private", "id_form": "zzz", "user_id": "u"},
            {"tier": "corpus", "id_form": ID_FORM_LC9, "user_id": "u"},
            {"id_form": ID_FORM_LC9, "user_id": "u"},
            {"tier": "private", "id_form": ID_FORM_LC9},
            {"tier": "private", "id_form": ID_FORM_LC9, "user_id": ""},
            {"tier": "private", "id_form": ID_FORM_LC9, "user_id": 7},
        ],
    )
    def test_every_other_row_is_legacy(self, meta: dict | None) -> None:
        assert new_form_owner(meta) is None


class TestChromaIdConflictError:
    def test_one_owner_free_text(self) -> None:
        err = ChromaIdConflictError("decisions", "foo")
        assert str(err) == tier_conflict_message("semantic", "decisions/foo")
        assert not isinstance(err, ManifestAuthorizationError)


class TestResolveWrite:
    def test_new_private_write_targets_the_owner_key(self) -> None:
        res = resolve_write("episodic", "k", "a", "private", target=None, bare=None)
        assert (res.kind, res.storage_key, res.row) == ("new", "a/k", None)

    def test_own_row_at_target(self) -> None:
        mine = _row("a/k", "private", "a")
        res = resolve_write("episodic", "k", "a", "private", target=mine, bare=None)
        assert res.kind == "own" and res.row == mine

    def test_other_users_legacy_bare_row_is_invisible_to_b(self) -> None:
        """C1: B over A's legacy row == B over an absent key."""
        a_legacy = _row("k", "private", "a")
        over_legacy = resolve_write(
            "episodic", "k", "b", "private", target=None, bare=a_legacy
        )
        over_absent = resolve_write(
            "episodic", "k", "b", "private", target=None, bare=None
        )
        assert over_legacy == over_absent
        assert over_legacy.kind == "new" and over_legacy.storage_key == "b/k"

    def test_own_live_legacy_row_copy_forwards(self) -> None:
        legacy = _row("k", "private", "a")
        res = resolve_write("episodic", "k", "a", "private", target=None, bare=legacy)
        assert res.kind == "legacy_own" and res.storage_key == "a/k"

    def test_own_tombstoned_legacy_row_is_new(self) -> None:
        legacy = _row("k", "private", "a", deleted_at_ms=5)
        res = resolve_write("episodic", "k", "a", "private", target=None, bare=legacy)
        assert res.kind == "new"

    def test_private_write_beside_a_corpus_row_reports_corpus(self) -> None:
        corpus = _row("k", "corpus", "curator")
        res = resolve_write("episodic", "k", "a", "private", target=None, bare=corpus)
        assert res.kind == "corpus" and res.storage_key == "a/k"

    def test_foreign_row_at_the_target_is_refused(self) -> None:
        """Fail-closed backstop: unreachable on Postgres after 034."""
        foreign = _row("b/k", "private", "a")
        with pytest.raises(CrossOwnerWriteError):
            resolve_write("episodic", "k", "b", "private", target=foreign, bare=None)

    def test_corpus_row_at_the_private_target_is_a_tier_conflict(self) -> None:
        odd = _row("b/k", "corpus", "b")
        with pytest.raises(CrossOwnerWriteError):
            resolve_write("episodic", "k", "b", "private", target=odd, bare=None)

    @pytest.mark.parametrize(
        "bare",
        [
            _row("k", "private", "a"),
            _row("k", "private", "curator"),
            _row("k", "private", "a", deleted_at_ms=5),
        ],
        ids=["other-live", "own-live", "other-tombstoned"],
    )
    def test_corpus_write_over_any_private_row_is_refused(self, bare: RowFacts) -> None:
        with pytest.raises(CrossOwnerWriteError):
            resolve_write("episodic", "k", "curator", "corpus", target=None, bare=bare)

    def test_corpus_write_over_corpus_row(self) -> None:
        corpus = _row("k", "corpus", "x")
        res = resolve_write("episodic", "k", "c", "corpus", target=None, bare=corpus)
        assert res.kind == "corpus" and res.storage_key == "k"

    def test_corpus_write_new(self) -> None:
        res = resolve_write("episodic", "k", "c", "corpus", target=None, bare=None)
        assert (res.kind, res.storage_key) == ("new", "k")

    def test_unknown_tier_rejected(self) -> None:
        with pytest.raises(ValueError):
            resolve_write("episodic", "k", "a", "shared", target=None, bare=None)

    def test_invalid_sub_refused_before_any_decision(self) -> None:
        with pytest.raises(InvalidPrivateSubjectError):
            resolve_write("episodic", "k", "a/b", "private", target=None, bare=None)


class TestRefusalText:
    def test_one_text_names_no_owner(self) -> None:
        texts = set()
        for bare in (
            _row("k", "private", "user-secret-owner"),
            _row("k", "private", "curator"),
        ):
            with pytest.raises(CrossOwnerWriteError) as exc:
                resolve_write("episodic", "k", "c", "corpus", target=None, bare=bare)
            texts.add(str(exc.value))
        assert texts == {tier_conflict_message("episodic", "k")}
        assert "user-secret-owner" not in next(iter(texts))
        assert "curator" not in next(iter(texts))

    def test_foreign_target_uses_the_same_text(self) -> None:
        with pytest.raises(CrossOwnerWriteError) as exc:
            resolve_write(
                "episodic",
                "k",
                "b",
                "private",
                target=_row("b/k", "private", "a"),
                bare=None,
            )
        assert str(exc.value) == tier_conflict_message("episodic", "k")


class TestResolveRead:
    def test_owner_key_first(self) -> None:
        mine = _row("a/k", "private", "a")
        legacy = _row("k", "private", "a")
        res = resolve_read("a", target=mine, bare=legacy)
        assert res is not None and res.kind == "own" and res.row == mine

    def test_own_legacy_row_second(self) -> None:
        legacy = _row("k", "private", "a")
        res = resolve_read("a", target=None, bare=legacy)
        assert res is not None and res.kind == "legacy_own"

    def test_own_tombstoned_legacy_still_readable(self) -> None:
        legacy = _row("k", "private", "a", deleted_at_ms=3)
        assert resolve_read("a", target=None, bare=legacy) is not None

    def test_other_users_legacy_row_is_not_found(self) -> None:
        """Dual lookup owner equality: B GET on A's legacy row == absent."""
        assert resolve_read("b", target=None, bare=_row("k", "private", "a")) is None

    def test_corpus_row_visible_to_everyone(self) -> None:
        res = resolve_read("b", target=None, bare=_row("k", "corpus", "c"))
        assert res is not None and res.kind == "corpus"

    def test_foreign_row_at_the_owner_key_is_not_found(self) -> None:
        assert resolve_read("b", target=_row("b/k", "private", "a"), bare=None) is None

    def test_nothing(self) -> None:
        assert resolve_read("a", target=None, bare=None) is None
