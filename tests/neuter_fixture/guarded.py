"""Toy guarded module for the neuter harness self-proofs (SPEC v3 §11).

Not shipped product code. It exists purely so the self-proofs exercise a
REAL git repository and a REAL pytest subprocess -- never a mock of
pytest's own behaviour -- while staying small enough to copy into a fresh
``tmp_path`` per proof.
"""

from __future__ import annotations


def guard_a(x: int) -> int:
    """Guard A: proof a's wrong-mapping trigger neuters this against `test_b`."""
    return x + 1


def guard_b(x: int) -> int:
    return x * 2


def seed() -> int:
    """Used by the ``seeded`` fixture. Proof i makes this raise, to prove a
    fixture crash classifies ``ERROR setup_or_teardown``, never ``RED``."""
    return 41


def slow(seconds: float = 0.0) -> None:
    """Proof j sleeps here past ``--timeout``."""
    import time

    time.sleep(seconds)


def raiser() -> None:
    """Called from a test body. Proof n's self-neuter makes this raise
    ``RuntimeError`` to prove a non-assertion call-phase exception
    classifies ``ERROR call_exception``, never ``RED``."""
    return None


def maybe_raise() -> None:
    """Proof n's ``pytest.raises`` sibling: the neuter removes this raise,
    so the test's ``pytest.raises(ValueError)`` DID NOT RAISE -- ``Failed``,
    assertion-shaped, RED."""
    raise ValueError("boom")
