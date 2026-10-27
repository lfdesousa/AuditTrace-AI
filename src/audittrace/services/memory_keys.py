"""Owner-in-the-key derivation and the shared write resolver (LC-9 WP1).

SECURITY (LC-9, cross-user private-row isolation). A private memory item is
identified by **its owner plus its logical key**, never by the logical key
alone. Before LC-9 the manifest key of a private row was the bare logical
key, ``UNIQUE (layer, key)`` was global, and ``authorize_write`` consulted
only corpus rows, so user B could overwrite, tombstone or squat user A's
private row (spec ``2026-10-26-SPEC-lc9-cross-user-private-row-isolation``
plus ADDENDUM-1, which wins where the two differ).

This module is the ONE place that maps stored <-> logical:

* ``physical_key`` / ``private_storage_key`` -- logical -> stored key of a
  NEW private manifest row (``{owner}/{logical}``).
* ``private_physical_id`` -- logical -> ChromaDB id of a private chunk
  (``{owner}__{id}``; always prepends, ADDENDUM-1 S5).
* ``logical_key`` / ``logical_id`` -- the inverse, decided by the row's OWN
  owner (never by the caller).
* ``deleted_key_candidates`` -- every storage key a Chroma chunk's manifest
  row can have (legacy bare + owner-keyed).
* ``resolve_write`` / ``resolve_read`` -- the pure decision over the (at
  most two) rows a write or read can touch. Both the Postgres service and
  its Mock twin feed rows in, so the two cannot drift.

Guards (each has a neuter in ``tests/test_lc9_*``):

* the owner prefix (``physical_key``);
* the sub rule (``check_sub``) and the length cap (``check_logical``);
* the bare key is consulted ONLY through predicates that exclude every other
  user's private row (``resolve_write``): own legacy row, or corpus row;
* a corpus write over ANY private row, and a private write over a row at its
  own target that is not its own, raise :class:`CrossOwnerWriteError` with
  ONE owner-free text (no existence oracle);
* ``logical_key`` strips only the row's own owner prefix.
"""

from __future__ import annotations

from dataclasses import dataclass

from audittrace.identity import UserContext

#: ``memory_items.key`` is ``String(255)``.
STORAGE_KEY_MAX = 255
#: ``memory_items.created_by_user_id`` is ``String(36)``.
SUB_MAX = 36
#: Longest logical key any caller may use: 255 - 36 - 1 = 218, independent of
#: the caller's actual sub (ADDENDUM-1 S1).
LOGICAL_KEY_MAX = STORAGE_KEY_MAX - SUB_MAX - 1

_TIERS = frozenset({"private", "corpus"})


class ManifestAuthorizationError(PermissionError):
    """Raised by ``record_create``/``record_update`` when a caller writes
    over an EXISTING **corpus**-tier manifest row without the shared-write
    authorization that row requires (SPEC security-memory-manifest-tier-
    authz, 2026-08-30 -- the M3-WU-D2-2 reviewer's cross-user corpus-hijack/
    hide finding).

    ``record_create``/``record_update`` are the shared manifest choke for
    every writer (the REST ``/memory/{episodic,procedural,semantic}``
    routes, the ``.md`` fold path via ``memory_md_manifest._flush_md_manifest``,
    and the ``mcp_write`` tools) -- raising HERE, not only at the route
    layer, means no caller can reach an unauthorized corpus overwrite by a
    path that forgets to duplicate the route-level scope check. Routes
    MUST map this to HTTP 403 (never let it fall through to a generic 500)
    -- see ``routes/memory.py``'s create/update handlers.

    LC-9 moved the class here (re-exported from
    ``audittrace.services.memory_manifest``) so the resolver's error types
    can subclass it without an import cycle.
    """


class CrossOwnerWriteError(ManifestAuthorizationError):
    """A write that would touch another user's private row, or that would
    re-stamp a row's tier. Maps to 403 through the existing
    ``ManifestAuthorizationError`` mapping. The message is ONE text for every
    cause and never names another owner (no existence oracle)."""


class InvalidPrivateSubjectError(ManifestAuthorizationError):
    """The caller's ``sub`` cannot be used as a private-storage prefix
    (would make prefixes non-injective). Maps to 403, never 500."""


class LogicalKeyTooLongError(ValueError):
    """A logical key longer than :data:`LOGICAL_KEY_MAX`. Routes map it to
    400 (ADDENDUM-1 S1, gate S2)."""


class ConcurrentManifestWriteError(Exception):
    """A concurrent same-item write won the ``UNIQUE (layer, key)`` race
    (typically two simultaneous copy-forwards of the caller's own legacy
    row). Nothing was written by the loser; a retry converges. Routes map it
    to 409 (retryable), never 500 (LC-9 WP1 review advisory A2).

    Deliberately NOT a ``ManifestAuthorizationError``/``RuntimeError``: it is
    neither a refusal nor the soft-deleted-row conflict some routes already
    catch as ``RuntimeError``."""


def tier_conflict_message(layer: str, logical: str) -> str:
    """The ONE refusal text for every cross-owner / tier-conflict cause."""
    return f"tier conflict: {layer} key {logical!r} is not available for this write"


def check_sub(sub: str) -> str:
    """Return *sub* if it is usable as an injective private-storage prefix.

    Rejects the empty string, anything over :data:`SUB_MAX`, a ``/`` or ``__``
    anywhere, and a trailing ``_`` (the last, with the ``__`` rule, is what
    makes ``{sub}/`` and ``{sub}__`` prefixes collision-free, measured in
    ADDENDUM-1 S5: 0 collisions)."""
    if not sub or len(sub) > SUB_MAX or "/" in sub or "__" in sub or sub.endswith("_"):
        raise InvalidPrivateSubjectError("subject not supported for private storage")
    return sub


def check_logical(logical: str) -> str:
    """Return *logical* if it fits under :data:`LOGICAL_KEY_MAX`."""
    if len(logical) > LOGICAL_KEY_MAX:
        raise LogicalKeyTooLongError(f"key longer than {LOGICAL_KEY_MAX} characters")
    return logical


def physical_key(owner_sub: str, logical: str) -> str:
    """Stored manifest key of a NEW private row: ``{owner}/{logical}``.

    *owner_sub* MUST be ``UserContext.user_id`` (the token's ``sub``), never
    a value taken from a request body or metadata."""
    return f"{check_sub(owner_sub)}/{check_logical(logical)}"


def private_storage_key(user: UserContext, logical: str) -> str:
    """:func:`physical_key` over a :class:`UserContext` (the spec's R1.1 form)."""
    return physical_key(user.user_id, logical)


def private_physical_id(user: UserContext, document_id: str) -> str:
    """ChromaDB id of a private chunk: ``{owner}__{id}``. ALWAYS prepends.

    An idempotent prefix would alias ``foo`` and ``user-a__foo`` (ADDENDUM-1
    S5, measured True), so there is none."""
    return f"{check_sub(user.user_id)}__{document_id}"


def logical_key(stored: str, *, tier: str | None, owner: str | None) -> str:
    """THE one stored -> logical key mapping (ADDENDUM-1 C3).

    Strips ``{owner}/`` only for a private row whose stored key starts with
    its OWN owner's prefix; a legacy bare key, a corpus key and an
    S3-discovered key come back unchanged."""
    if tier == "private" and owner and stored.startswith(owner + "/"):
        return stored[len(owner) + 1 :]
    return stored


def logical_id(physical: str, owner: str | None) -> str:
    """Inverse of :func:`private_physical_id` for the chunk's own owner."""
    if owner and physical.startswith(owner + "__"):
        return physical[len(owner) + 2 :]
    return physical


def deleted_key_candidates(
    collection: str, chunk_id: str, owner: str | None
) -> set[str]:
    """Storage keys a Chroma chunk's manifest row can have: the legacy bare
    ``{collection}/{chunk_id}`` and the owner-keyed
    ``{owner}/{collection}/{logical_id}`` (the latter only when the chunk id
    carries its owner's ``__`` prefix)."""
    out = {f"{collection}/{chunk_id}"}
    if owner and chunk_id.startswith(owner + "__"):
        out.add(f"{owner}/{collection}/{logical_id(chunk_id, owner)}")
    return out


@dataclass(frozen=True, slots=True)
class RowFacts:
    """The four columns the resolver decides on, backend-neutral."""

    key: str
    tier: str
    created_by_user_id: str
    deleted_at_ms: int | None


@dataclass(frozen=True, slots=True)
class Resolution:
    """Outcome of a resolve. ``kind``:

    * ``new`` -- nothing to touch; a write inserts at ``storage_key``.
    * ``own`` -- the caller's owner-keyed private row (``row``).
    * ``legacy_own`` -- the caller's OWN legacy bare-key private row (``row``);
      a write copy-forwards to ``storage_key``.
    * ``corpus`` -- the bare-key corpus row (``row``); the caller's corpus
      guard applies. For a private write ``storage_key`` is still the
      owner key (the corpus row is never re-stamped).
    """

    storage_key: str
    kind: str
    row: RowFacts | None


def resolve_write(
    layer: str,
    logical: str,
    owner: str,
    requested_tier: str,
    *,
    target: RowFacts | None,
    bare: RowFacts | None,
) -> Resolution:
    """Decide what a write of *logical* by *owner* at *requested_tier* touches.

    *target* is the row stored at the key the write TARGETS (``{owner}/K`` for
    private, ``K`` for corpus); *bare* is the row stored at the bare key ``K``
    (the same row as *target* for a corpus write). Only the row AT the target
    can make a private write ``own`` or refused; the bare key is read only
    through predicates that exclude every other user's private row
    (ADDENDUM-1 C1), so no other owner's row can enter the decision.

    Raises :class:`CrossOwnerWriteError` for ``foreign`` and ``tier_conflict``.
    """
    if requested_tier not in _TIERS:
        raise ValueError(f"invalid tier {requested_tier!r}")
    refusal = tier_conflict_message(layer, logical)
    if requested_tier == "corpus":
        # The target IS the bare key. ANY private row there (any owner, live
        # or tombstoned, the caller's own included) is a tier conflict.
        row = bare
        if row is None:
            return Resolution(logical, "new", None)
        if row.tier == "private":
            raise CrossOwnerWriteError(refusal)
        return Resolution(logical, "corpus", row)

    storage = physical_key(owner, logical)
    if target is not None:
        if target.tier != "private":
            raise CrossOwnerWriteError(refusal)
        if target.created_by_user_id != owner:
            # Fail-closed backstop; unreachable on Postgres after 034 (CHECK).
            raise CrossOwnerWriteError(refusal)
        return Resolution(storage, "own", target)
    if (
        bare is not None
        and bare.tier == "private"
        and bare.created_by_user_id == owner
        and bare.deleted_at_ms is None
    ):
        return Resolution(storage, "legacy_own", bare)
    if bare is not None and bare.tier == "corpus":
        return Resolution(storage, "corpus", bare)
    # No row, a tombstoned own legacy row, or another user's private row: all
    # indistinguishable to the caller (no existence oracle).
    return Resolution(storage, "new", None)


def resolve_read(
    owner: str,
    *,
    target: RowFacts | None,
    bare: RowFacts | None,
) -> Resolution | None:
    """Dual-lookup read order: the caller's owner key, then the caller's OWN
    legacy bare row (owner equality, live or tombstoned -- reads return
    tombstones), then a corpus row. ``None`` when nothing the caller may see
    exists (another user's private row is ``None`` too, never a refusal)."""
    if (
        target is not None
        and target.tier == "private"
        and target.created_by_user_id == owner
    ):
        return Resolution(target.key, "own", target)
    if bare is not None and bare.tier == "private" and bare.created_by_user_id == owner:
        return Resolution(bare.key, "legacy_own", bare)
    if bare is not None and bare.tier == "corpus":
        return Resolution(bare.key, "corpus", bare)
    return None
