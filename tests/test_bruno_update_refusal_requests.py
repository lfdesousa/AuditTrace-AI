"""#61 WU-2: the two shipped Bruno requests the live legs run (ADDENDUM-2 A2.7).

A request that asserts a text the route does not give fails on the cluster, in
front of the operator; one that names an undefined variable fails the same way.
These pins tie each request's assertions to the exact refusal texts the
ratified RED file pins for the route, and its variables to both environments.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_memory_update_corpus_fold_chunk import FOLD_REFUSAL, SOFT_DELETED

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
FOLD = "memory/semantic/04b-update-corpus-fold-chunk-is-refused.bru"
DELETED = "memory/semantic/04c-update-soft-deleted-is-refused.bru"
REQUESTS = [FOLD, DELETED]


def _text(request: str) -> str:
    return (COLLECTION / request).read_text()


def _env(name: str) -> dict[str, str]:
    text = (COLLECTION / "environments" / f"{name}.bru").read_text()
    return dict(re.findall(r"^  (\w+): (.*)$", text, re.M))


def _contained(request: str) -> list[str]:
    """The strings the request's tests assert the detail contains."""
    found = re.findall(r"\.to\.contain\(\s*\"(.*?)\"\s*\)", _text(request), re.S)
    assert found, f"{request} asserts no detail text"
    return found


@pytest.mark.parametrize("request_path", REQUESTS)
def test_the_request_is_a_put_that_expects_409(request_path: str) -> None:
    text = _text(request_path)
    assert re.search(r"^put \{", text, re.M)
    assert "to.equal(409)" in text


@pytest.mark.parametrize("env", ["cluster", "local"])
@pytest.mark.parametrize("request_path", REQUESTS)
def test_every_variable_of_the_request_is_defined(request_path: str, env: str) -> None:
    used = set(re.findall(r"\{\{(\w+)\}\}", _text(request_path)))
    assert not used - set(_env(env)) - {"accessToken"}


def test_the_fold_request_asserts_the_text_the_route_gives() -> None:
    (expected,) = _contained(FOLD)
    assert expected in FOLD_REFUSAL.format(id="any")


def test_the_soft_deleted_request_asserts_the_text_the_route_gives() -> None:
    (expected,) = _contained(DELETED)
    assert expected in SOFT_DELETED
