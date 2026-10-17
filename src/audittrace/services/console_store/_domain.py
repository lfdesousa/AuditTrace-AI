"""``ConsoleDomain[T]`` — what a domain DECLARES, and nothing more.

A domain is a small descriptor handed TO a sealed store (composition, not
inheritance): it names its ORM model, its client key, its writable value
columns, its ordering, and a few overridable hooks. It never receives the
store, a session factory, an ``AsyncSession`` or an ORM row — every hook
sees plain ``dict`` snapshots and returns plain ``dict``s, which the base
validates before they touch a row. That is the "narrowed seam" of ADDENDUM
A §2: there is no attribute on a domain through which the unguarded
resource can be reached, because the domain is never given one.

What a hook can and cannot do:

* :meth:`cap` — an optional per-user cap on ACTIVE rows. The COUNT that
  enforces it is the BASE's user-scoped aggregate; a domain never writes
  its own (``lesson-aggregate-queries-must-be-user-scoped-20260913``).
* :meth:`defaults` / :meth:`merge` — shape the VALUE columns of an insert /
  update. Output naming a RESERVED column is refused
  (:class:`ConsoleStoreForbiddenFieldError`); output naming an unknown
  column is refused (:class:`ConsoleStoreDomainError`).
* :meth:`equality_filters` — extra ``column == value`` predicates composed
  INTO the guarded query with AND. It can only NARROW; it cannot name a
  reserved column, so it cannot widen past ``user_sub``.
* :meth:`to_item` — serialize a row snapshot (a plain dict copy) to the
  domain's item type ``T``.

The template members (``snapshot_columns``, ``has_session_id``,
``order_columns``, ``order_directions``) are sealed: a subclass redefining
them is refused at class-creation time.

**F1 (fix round 1, 2026-09-15): the descriptor itself is sealed, not just
the pointer to it.** ``ConsoleStoreBase.domain`` hands out a live reference
to the exact instance ``validate_domain()`` checked at construction. Before
this fix that instance was ordinary — ``store.domain.value_columns =
(*store.domain.value_columns, "trace_id")`` reassigned the instance
attribute, superseding the one-time check with a descriptor that never
passed it, and ``_base.py``'s insert loop then silently overwrote the
base's own ``trace_id`` stamp with ``None`` (M5 / EU AI Act Art 12). A read-
only *property* protects the binding; it never protects the referent. Every
:class:`ConsoleDomain` instance refuses an ORDINARY attribute set or delete
for its lifetime (``__setattr__`` / ``__delattr__`` below) — handing out an
immutable object by reference is safe, so ``ConsoleStoreBase.domain`` needs
no further narrowing. This is NOT unconditional: a subclass that redefines
``__setattr__``/``__delattr__`` itself would have escaped it entirely — see
the **third hop** below (SPEC ADDENDUM C, fix round 2) for why that route
is now closed too, and the disclosed residuals (``object.__setattr__``,
direct ``__dict__`` writes) that remain regardless.

**Why hand-written, not ``@dataclass(frozen=True)``** (the shape the fix
spec recommends as a starting point): tried first, and falsified by direct
reproduction before shipping — not assumed to work. Every column here
(``value_columns``, ``key_columns``, ``order_by``, ...) is a ``ClassVar``,
so a domain's dataclass ``fields()`` tuple is ALWAYS empty; CPython's
generated frozen ``__setattr__`` is ``if type(self) is cls or name in
{<fields>}: raise FrozenInstanceError(...); else: super(cls, self).
__setattr__(name, value)`` (``dataclasses._frozen_get_del_attr``), and with
zero fields the ``or name in {...}`` half never fires, so EVERY subclass
instance (i.e. every real domain — ``ConsoleDomain`` itself is never
instantiated) falls through to the ``super(cls, self)`` branch and the
assignment SUCCEEDS. Adding ``slots=True`` makes it worse, not better: it
rebuilds the class object after generating ``__setattr__``, so the ``cls``
the generated function closed over is the discarded pre-slots class — even
a direct instance of the (final) class then hits ``TypeError: super(type,
obj): obj must be an instance or subtype of type`` instead of the intended
``FrozenInstanceError``. Both reproduced with a minimal zero-field example
before this module was written (`lesson-unpinnable-claim-check-your-own-
techniques-20260915` — a claim about a technique is a claim requiring
proof, the same class of error this lesson exists to stop). The hand-
written seal below is unconditional for every instance regardless of
subclass or field count, and mirrors the already-reviewed, already-PASSED
pattern in :class:`~audittrace.services.console_store._base.
ConsoleStoreBase.__setattr__`.

Residual, disclosed (same family as ``ConsoleStoreBase``'s): direct
``object.__setattr__(domain, name, value)`` or a ``__dict__`` write still
succeeds — deliberate circumvention, not the ordinary-Python hurry-mode
path this seal exists for.

**Second hop, found while enumerating THIS round (not the original F1
report, a self-found extension of the same principle): the class-level
ClassVar itself.** ``ConsoleDomain.__setattr__`` above is an INSTANCE
method — Python only calls it for ``domain.value_columns = ...``. It is
NOT called for ``WidgetDomain.value_columns = (...)`` (a class-level
reassignment of the ClassVar), because setting an attribute on a class
object is dispatched to the *metaclass's* ``__setattr__``, and
``ConsoleDomain`` had none (plain ``ABCMeta``). That one-liner is exactly
as "ordinary Python" as the original F1 exploit line, and its blast
radius is WORSE: it mutates the column tuple for every store built with
that domain CLASS, present and future, not just the one instance a caller
holds a reference to — and, FOR AN ORDINARY ``ClassVar``, ``self._domain.
value_columns`` always resolves to the class attribute this hop mutates (a
domain instance never legitimately carries its own instance override — the
constructor never sets one), so the mutation is invisible to any check
that only inspects instances. **Corrected (SPEC ADDENDUM D, fix round 3):
"always resolves to the class attribute" is a claim about an ORDINARY
ClassVar, not about the READ itself** — a ``property``/``__getattribute__``
override (the fifth hop below) makes ``self._domain.value_columns``
resolve to a COMPUTATION every time, not a class-dict lookup, and this
hop's fix (read once, cache on the STORE, never re-read) does not depend
on the corrected claim being true either way.
:class:`_DomainMeta` closes this the same way :class:`~audittrace.
services.console_store._base._SealedMeta` closes the equivalent class-
level monkeypatch for stores: refuse a class-level ``setattr``/
``delattr`` naming a domain descriptor attribute. (An unconditional
class-level block was tried first and falsifies immediately — ``ABCMeta.
__new__`` itself does ``cls.__abstractmethods__ = frozenset(...)`` AFTER
the class object exists, and ``typing``'s ``_generic_init_subclass`` does
the same for ``cls.__parameters__``; blocking every name breaks ordinary
class creation for every subclass. The block is therefore by EXPLICIT
NAME, mirroring ``SEALED_STORE_MEMBERS``, not "everything".)

**Third hop (SPEC ADDENDUM C, fix round 2, 2026-09-17): the SANCTIONED
extension mechanism itself — an ordinary domain subclass overriding the
seal's own dunders.** A domain legitimately lives OUTSIDE this package (it
is *the* extension point), and ``__init_subclass__`` only sealed
``{snapshot_columns, has_session_id, order_columns, order_directions}`` —
``__setattr__``/``__delattr__`` were not in that set, so an ordinary
subclass, no monkeypatch, no ``object.__setattr__`` call, no metaclass
swap, just::

    class UnsealedDomain(WidgetDomain):
        def __setattr__(self, name, value):
            self.__dict__[name] = value

defeated the FIRST hop entirely, and the verbatim F1 exploit line then
reached ``_base.py``'s insert loop again in full (raw-DB witness: honest
row carries the real span id, attacked row's ``trace_id`` is ``None``).
``_SEALED_DOMAIN_MEMBERS`` below now includes ``__setattr__`` and
``__delattr__`` themselves, so ``_refuse_redefinition`` (already the
mechanism that seals the four template members) refuses the ``class``
statement above at CREATION time — the control already existed; the
member list was simply incomplete. **The lesson to internalise, not just
patch: the sanctioned extension point is the PRIMARY attack surface, not
an afterthought — enumerate mutations as a subclass author would write
them, legitimate API first, exotica second.**

**Fourth hop, same round: the metaclass swap.** ``_DomainMeta`` guarded
``_SEALED_DOMAIN_CLASS_ATTRS`` but not ``__setattr__``, ``__delattr__`` or
``__class__`` THEMSELVES — so ``WidgetDomain.__class__ = ABCMeta`` (an
ordinary one-line class-level reassignment) silently removed
``_DomainMeta`` from the class's dispatch, after which
``WidgetDomain.value_columns = (...)`` — the exploit Guard D exists to
refuse — succeeded again. A guard whose OWN hooks are reassignable is not
a guard; it is a default. ``_SEALED_DOMAIN_CLASS_ATTRS`` now names
``__setattr__``, ``__delattr__`` and ``__class__`` explicitly, so
reassigning any of them at the class level is refused the same way as
every other sealed descriptor attribute.

Residual, disclosed (same family as every seal in this module):
``object.__setattr__``/``type.__setattr__`` called directly, and a raw
``__dict__``/class-``__dict__`` write, still work — deliberate
circumvention, not the ordinary-Python hurry-mode path these seals exist
for.

**Fifth hop (SPEC ADDENDUM D, fix round 3, 2026-09-17): sealing MUTATION
is not sealing VALUE — a stateful ``property``/``__getattribute__`` needs
no mutation at all.** Every guard above (A/B/D + Guard C in ``_base.py``)
answers "can ``value_columns`` be REASSIGNED after ``validate_domain()``
ran?" None of them answers "does ``value_columns`` return the SAME value
on every read?" ``property``/``__getattribute__``/``__get__`` were never
in ``_SEALED_DOMAIN_MEMBERS`` (they are not attribute NAMES to refuse
redefining — a domain legitimately may not define them anyway, since
``ConsoleDomain`` declares ``value_columns`` etc. as plain ``ClassVar``
annotations, not descriptors), so an ordinary subclass — the sanctioned
extension point, same lesson as the third hop — can shape ``value_columns``
as a property with internal counter state::

    class TocTouDomain(WidgetDomain):
        _n = 0
        @property
        def value_columns(self) -> tuple[str, ...]:
            cls = type(self); n = cls.__dict__["_n"]
            type.__setattr__(cls, "_n", n + 1)
            return ("payload", "priority", "trace_id") if n >= 7 else ("payload", "priority")

``value_columns`` was read 9 times across one construction + upsert
before this fix: ``validate_domain()``'s reserved-column check saw the
SAFE 2-tuple; ``_insert_values``'s row-assignment loop, several reads
later, saw the HOSTILE 3-tuple, and silently overwrote the base's own
``trace_id`` stamp with ``None`` — the verbatim F1 harm, with no
monkeypatch, no ``object.__setattr__``, no metaclass swap, reproduced via
both a ``property`` and a ``__getattribute__`` override (the defect is
read-NONATOMICITY, not one descriptor trick). An attempt to escalate this
to a cross-user READ (a time-varying ``model`` property remapping
``user_sub``) FAILED — ``_scoped_select`` binds the compared value from
``_read_sub()`` (token-derived), so the predicate cannot be steered; F6's
harm is traceability (M5 / EU AI Act Art 12), not isolation.

**The fix: read once, cache on the STORE, never re-read.** Mirroring the
pattern ``_filters()``/``_cap()`` already use for the domain's DYNAMIC
hooks (invoke once per call, carry the result) — generalised here to the
five STATIC declarative surfaces (``name``, ``model``, ``key_columns``,
``value_columns``, ``order_by``, plus the derived ``has_session_id``/
``snapshot_columns``/``order_columns``/``order_directions``/
``default_list_limit``/``max_list_limit``): :func:`validate_domain` now
reads each of these EXACTLY ONCE (into a local, never re-fetched even
within its own body), validates the captured values, and returns them as
a :class:`DomainContract` — an immutable, plain-data snapshot.
``ConsoleStoreBase.__init__`` stores it as ``self._contract`` (write-once,
same generalised instance-attribute guard as ``self._domain``/
``self._sessions``). Every sealed template helper in ``_base.py`` and
every implementation (``_postgres.py``, ``_mock.py``) reads
``self._contract.X`` for these five/eleven fields — NEVER
``self._domain.X`` again. This is R1's "read it once ... and pass that
value forward to every consumer" branch, chosen over "re-validate
atomically at use" because these surfaces are genuinely CONSTANT for a
store's lifetime (unlike ``cap()``/``defaults()``/``merge()``/
``equality_filters()``/``to_item()``, which stay live-invoked via
``self._domain.<hook>()`` on every call, exactly as before — those ARE
meant to vary per call, and their OUTPUT is validated against the
CACHED ``self._contract.key_columns``/``value_columns``, never against a
fresh ``self._domain.key_columns``/``value_columns`` read).

**Why this also answers R2 (constancy) without a new guard.** A
``property``/``__getattribute__`` override can still make
``self._domain.value_columns`` non-constant — nothing added here refuses
that shape at class-creation time (deliberately: see "check the siblings"
below for why a structural refusal was rejected). What closes the harm is
that NOTHING downstream of ``validate_domain()`` ever performs that read
again: the property's return value at the single moment
``validate_domain()`` calls it is the value CAPTURED and PINNED in
``self._contract``, forever, for that store instance. There is no "compare
on use, refuse on drift" step because there is no later USE of the live
descriptor to compare against — pinning by value and never re-reading is
the strongest instance of R2's "pin it by value" option, not a weaker one.
A structural refusal (reject a domain whose ``value_columns`` is a
descriptor) was considered and rejected: it would forbid a LEGITIMATE
future domain that computes a value column list from, e.g., a frozen
class-level registry via a ``classmethod``-backed ``property`` — the
read-once-and-carry fix protects the base regardless of whether the
domain author's descriptor is honest or hostile, so the extra restriction
would cost real flexibility for zero additional safety.

**The four siblings, audited on the SAME axis (R1's own instruction —
"the reviewer found no cross-user read through them; that is not the same
as proving them safe"):** ``key_columns``, ``model``, ``order_by`` and
``has_session_id`` are read off ``self._domain`` in exactly the same
"live attribute access, no caching" shape ``value_columns`` was — grep
confirms ``key_columns`` alone is read from four separate call sites
across ``_base.py``/``_postgres.py`` for a single ``batch_get``. **DONE,
not merely disclosed:** all four are pulled into the SAME
:class:`DomainContract` capture, by the SAME single-read mechanism, for
the SAME reason (R1 does not scope its "read once and carry" instruction
to ``value_columns`` alone) — see ``_base.py``'s module docstring for the
full per-surface table (both axes: what can be MUTATED and what pins the
CONSTANT read) and the fix-round-3 evidence file for the reachability
count each surface had before this fix.

Residual, disclosed (unchanged in kind from every seal above): a future
accessor that builds a :class:`DomainContract` BY HAND (bypassing
:func:`validate_domain` entirely) and hands it to
``ConsoleStoreBase.__init__`` would recreate this exact class of gap for
whatever it captures wrong — no code can protect against a future
caller who chooses not to call the validator. ``_refuse_reserved_value_
columns`` (``_base.py``) stays LOAD-BEARING for exactly that future path,
now checking the CACHED ``self._contract.value_columns`` (the same value
every other consumer in this call actually uses), not the live domain.
"""

from __future__ import annotations

from abc import ABC, ABCMeta, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar, Generic, TypeVar, final

from audittrace.services.console_store._context import REQUIRED_MODEL_COLUMNS
from audittrace.services.console_store._cursor import Direction
from audittrace.services.console_store._errors import ConsoleStoreSealedError
from audittrace.services.console_store._sealing import seal_members

# Reserved columns a domain MAY order by (always non-null, so paging is
# total). Key columns are also allowed; value columns are not (nullable
# values make keyset comparison undefined).
ORDERABLE_RESERVED: frozenset[str] = frozenset({"id", "created_at_ms", "updated_at_ms"})

# SPEC ADDENDUM C R1 (fix round 2): __setattr__/__delattr__ are sealed
# TEMPLATE MEMBERS in their own right — a domain subclass overriding
# either escapes the instance-level seal below entirely (the "third hop"
# in the module docstring). _refuse_redefinition (via seal_members in
# __init_subclass__) already refuses a subclass that redefines a name in
# this set; adding the seal's own dunders here is what closes that hop,
# with no new mechanism.
_SEALED_DOMAIN_MEMBERS: frozenset[str] = frozenset(
    {
        "snapshot_columns",
        "has_session_id",
        "order_columns",
        "order_directions",
        "__setattr__",
        "__delattr__",
    }
)

# The declarative ClassVars every domain declares (checked by
# validate_domain() at construction) PLUS the sealed template members
# above PLUS `__class__` (SPEC ADDENDUM C R2/R4, fix round 2: reassigning
# the CLASS's own metaclass — ``WidgetDomain.__class__ = ABCMeta`` — is an
# ordinary one-line class-level setattr that removes _DomainMeta from
# dispatch entirely, the "fourth hop" in the module docstring; it is not a
# declared ClassVar, so it is added explicitly here rather than by
# inheriting from _SEALED_DOMAIN_MEMBERS). All of these are refused as a
# CLASS-level (post-creation) setattr/delattr by _DomainMeta below. NOT
# every class attribute is blocked (that breaks ABCMeta / typing
# machinery, proven false directly, see the docstring); only these, by
# explicit name.
_SEALED_DOMAIN_CLASS_ATTRS: frozenset[str] = _SEALED_DOMAIN_MEMBERS | {
    "name",
    "model",
    "key_columns",
    "value_columns",
    "order_by",
    "default_list_limit",
    "max_list_limit",
    "__class__",
}


class _DomainMeta(ABCMeta):
    """Refuse a CLASS-level ``setattr``/``delattr`` naming a domain
    descriptor attribute (``WidgetDomain.value_columns = (...)``), OR the
    seal's own hooks (``__setattr__``, ``__delattr__``, ``__class__``) —
    closing the second AND fourth hops of the F1 mutation surface (see the
    module docstring). This is a fixed, named set rather than a block on
    every class attribute (the latter breaks ABCMeta/typing machinery,
    proven false directly, see the docstring), and the block cannot fire
    during ordinary ``class Foo(ConsoleDomain): value_columns = (...)``
    declaration (the namespace dict is built BEFORE ``type.__new__``
    creates the class object; this metaclass never sees that as a
    ``setattr`` call, only a REASSIGNMENT after the class already exists).

    Falsifiable: neuter this and ``WidgetDomain.value_columns = (*…,
    "trace_id")`` (ordinary Python, no dunder, no ``type.__setattr__``
    call written out) succeeds and nulls ``trace_id`` on every subsequent
    write through every store built with that domain class; separately,
    ``WidgetDomain.__class__ = ABCMeta`` then re-opens the SAME exploit
    line by removing this metaclass from dispatch — a cross-user read
    through the public API (SPEC ADDENDUM C R2/R4), not merely a
    class-attribute inconvenience.
    """

    def __setattr__(cls, name: str, value: Any) -> None:
        if name in _SEALED_DOMAIN_CLASS_ATTRS:
            raise ConsoleStoreSealedError(
                f"{cls.__qualname__}.{name} is a domain descriptor attribute "
                "and cannot be reassigned on the class after definition"
            )
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if name in _SEALED_DOMAIN_CLASS_ATTRS:
            raise ConsoleStoreSealedError(
                f"{cls.__qualname__}.{name} is a domain descriptor attribute "
                "and cannot be deleted from the class after definition"
            )
        super().__delattr__(name)


# PEP 484 TypeVar (not PEP 695 native syntax, per the ratified spec's own
# "Generic ABC via typing.Generic[T] (PEP 484)" instruction): the repo's
# pre-commit mypy hook is pinned to v1.8.0, which predates PEP 695 support
# and cannot parse `class Foo[T]:` at all — it silently treats the class as
# non-generic, so every parameterized use (`ConsoleDomain[dict[str, Any]]`)
# then fails with "expects no type arguments". This is the actual
# mechanically-enforced gate (CI has no separate mypy step; the local
# pre-commit hook IS the gate), so PEP 484 syntax is not just spec-faithful
# here, it is required for `git commit` to succeed at all.
T = TypeVar("T")


class ConsoleDomain(Generic[T], ABC, metaclass=_DomainMeta):  # noqa: UP046 - see the T = TypeVar comment above
    """Declarative descriptor of one console domain (see module docstring)."""

    name: ClassVar[str]
    model: ClassVar[type[Any]]
    key_columns: ClassVar[tuple[str, ...]]
    value_columns: ClassVar[tuple[str, ...]]
    order_by: ClassVar[tuple[tuple[str, Direction], ...]]
    default_list_limit: ClassVar[int] = 25
    max_list_limit: ClassVar[int] = 200

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        seal_members(cls, sealed_members=_SEALED_DOMAIN_MEMBERS)

    @final
    def __setattr__(self, name: str, value: Any) -> None:
        """F1: refuse every ORDINARY instance attribute set — see the
        module docstring for the defect this closes and why this is
        hand-written rather than ``@dataclass(frozen=True)``. There is no
        legitimate instance-attribute write to protect: every real domain
        (this class is never instantiated directly) declares its columns,
        order and hooks entirely at the CLASS level, which this does not
        touch — ``class Foo(ConsoleDomain): value_columns = (...)`` sets a
        class attribute via ``type.__new__``, never this method. Sealed as
        a TEMPLATE MEMBER itself (``_SEALED_DOMAIN_MEMBERS`` above, SPEC
        ADDENDUM C R1): a subclass may not redefine this method, closing
        the "third hop" the module docstring documents. Disclosed
        residual, same family as every seal in this module: direct
        ``object.__setattr__``/``__dict__`` writes still bypass it."""
        raise ConsoleStoreSealedError(
            f"{type(self).__qualname__}.{name} is part of a validated "
            "domain descriptor and cannot be set after class definition"
        )

    @final
    def __delattr__(self, name: str) -> None:
        raise ConsoleStoreSealedError(
            f"{type(self).__qualname__}.{name} is part of a validated "
            "domain descriptor and cannot be deleted"
        )

    # ── overridable hooks ────────────────────────────────────────────────

    def cap(self) -> int | None:
        """Maximum ACTIVE rows per user, or ``None`` for uncapped."""
        return None

    def defaults(self, key: Mapping[str, Any]) -> Mapping[str, Any]:
        """Value-column defaults for a brand-new row (``key`` is read-only
        context). Columns not defaulted and not supplied insert as
        ``None``."""
        return {}

    def merge(
        self, current: Mapping[str, Any], patch: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """How an update applies: ``current`` holds the row's value columns,
        ``patch`` the caller's. Default: the patch wins for the columns it
        names."""
        return {**current, **patch}

    def equality_filters(self) -> Mapping[str, Any]:
        """Extra ``column == value`` predicates AND-ed into every query."""
        return {}

    @abstractmethod
    def to_item(self, row: Mapping[str, Any]) -> T:
        """Serialize a row snapshot (plain dict) into the item type."""

    # ── sealed template members ──────────────────────────────────────────

    @final
    def snapshot_columns(self) -> tuple[str, ...]:
        """Every column the base reads off a row: the reserved columns the
        model carries, then key, then value columns."""
        reserved = tuple(c for c in REQUIRED_MODEL_COLUMNS) + (
            ("session_id",) if self.has_session_id() else ()
        )
        return reserved + tuple(self.key_columns) + tuple(self.value_columns)

    @final
    def has_session_id(self) -> bool:
        return hasattr(self.model, "session_id")

    @final
    def order_columns(self) -> tuple[str, ...]:
        return tuple(column for column, _ in self.order_by)

    @final
    def order_directions(self) -> tuple[Direction, ...]:
        return tuple(direction for _, direction in self.order_by)
