"""Test module paired with ``guarded.py`` for the neuter harness self-proofs
(SPEC v3 §11). Copied into a throwaway git repo per proof; never run
directly as part of this repo's own suite (see the exclusion in
``pyproject.toml``'s ``testpaths``/collection scope -- this directory lives
outside ``tests/test_*.py`` naming so pytest never collects it here)."""

from __future__ import annotations

import pytest
from guarded import guard_a, guard_b, maybe_raise, raiser, seed, slow


@pytest.fixture
def seeded():
    return seed()


def test_a():
    assert guard_a(1) == 2


def test_b():
    assert guard_b(2) == 4


def test_uses_seed(seeded):
    assert seeded == 41


def test_uses_slow():
    slow(0.01)
    assert True


def test_uses_raiser():
    raiser()
    assert True


def test_maybe_raise_shape():
    with pytest.raises(ValueError):
        maybe_raise()
