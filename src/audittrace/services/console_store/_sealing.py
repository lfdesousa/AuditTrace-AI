"""Runtime backing for ``@final`` — Python has no ``final`` and no true
privates, so the base seals itself by REFUSING the class statement.

Two checks, both run from ``__init_subclass__`` (i.e. while the ``class``
statement is executing, before the class object exists anywhere):

1. **Package fence.** The frame executing the subclass body must live in a
   file inside the ``console_store`` package directory. This is a FILE
   check, not a ``__module__`` check — a hostile class body setting
   ``__module__ = "audittrace.services.console_store._postgres"`` is still
   refused, and so is ``types.new_class`` (its frame is in ``types.py``).
2. **Sealed members.** Even an in-package subclass may not redefine a
   sealed template member (the guarded query builder, the stamping helpers).

What this does NOT close, stated plainly (D14 lesson: never claim more than
the code delivers): ``exec(compile(src, <forged in-package filename>))``
forges the frame's filename and passes the fence. That is deliberate
circumvention of a security control, outside the "someone in a hurry" mode
this fence exists for; it is disclosed in the build record's surface list.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from types import FrameType

from audittrace.services.console_store._errors import ConsoleStoreSealedError

PACKAGE_DIR: Path = Path(__file__).resolve().parent


_MAX_FRAME_WALK = 12

# Interpreter/stdlib frames that sit between the ``class`` statement and
# ``__init_subclass__`` (metaclass ``__new__`` in ``abc``, ``typing``
# generic machinery, ``types.new_class``). They are skipped, so the first
# remaining frame is the one that executed the ``class`` statement — its
# filename is where the subclass is written.
_MACHINERY_FILENAMES: frozenset[str] = frozenset({"abc.py", "typing.py", "types.py"})


def _is_machinery(frame: FrameType) -> bool:
    filename = frame.f_code.co_filename
    if filename.startswith("<frozen"):
        return True
    if frame.f_code.co_name == "__init_subclass__":
        return True
    return Path(filename).name in _MACHINERY_FILENAMES


def _class_statement_file() -> Path | None:
    """Filename of the frame that executed the ``class`` statement, or
    ``None`` when the stack has no such frame (refused, fail-closed).

    By the time ``__init_subclass__`` runs, the class BODY frame has already
    returned; what remains above the metaclass machinery is the frame
    holding the ``class`` statement itself (a module, a function, or the
    PEP 695 ``<generic parameters of …>`` frame — all in the statement's
    file). That file is what the package fence compares.
    """
    frame: FrameType | None = sys._getframe(2)  # noqa: SLF001 - CPython frame introspection
    walked = 0
    while frame is not None and walked < _MAX_FRAME_WALK:
        if not _is_machinery(frame):
            return Path(frame.f_code.co_filename).resolve()
        frame = frame.f_back
        walked += 1
    return None


def seal_subclass(cls: type, *, sealed_members: Iterable[str]) -> None:
    """Refuse ``cls`` unless its class body executes inside the package and
    redefines none of ``sealed_members``.

    Falsifiable: ``tests/test_console_store_sealed_classes.py`` defines subclasses
    outside the package and asserts the class statement itself raises;
    neuter this function into a no-op and those hostile subclasses come to
    life and read another user's row.
    """
    body_file = _class_statement_file()
    if body_file is None or body_file.parent != PACKAGE_DIR:
        raise ConsoleStoreSealedError(
            f"{cls.__qualname__}: subclassing a sealed console-store class "
            "outside audittrace.services.console_store is refused — "
            "parameterize a store with a ConsoleDomain instead"
        )
    _refuse_redefinition(cls, sealed_members)


def _refuse_redefinition(cls: type, sealed_members: Iterable[str]) -> None:
    """A sealed member may be DEFINED once (by whichever class introduces
    it) but never REDEFINED by a class further down the MRO."""
    redefined = sorted(
        name
        for name in sealed_members
        if name in cls.__dict__
        and any(name in base.__dict__ for base in cls.__mro__[1:])
    )
    if redefined:
        raise ConsoleStoreSealedError(
            f"{cls.__qualname__}: sealed member(s) may not be overridden: "
            f"{', '.join(redefined)}"
        )


def seal_members(cls: type, *, sealed_members: Iterable[str]) -> None:
    """Refuse ``cls`` if it redefines any sealed template member. Used by
    :class:`ConsoleDomain`, whose subclasses legitimately live OUTSIDE the
    package (a domain is the extension point) but may still not replace the
    base's template members."""
    _refuse_redefinition(cls, sealed_members)
