"""MEMORY-SERVER-SILENT-TRUNCATION r3 (N1): the body-limit middleware bounds
only the verbs in its set, so every memory route that takes a request body must
use one of them. This pins that invariant structurally, from the real app's
OpenAPI document, so a new GET-with-body route or a PATCH route fails here
instead of arriving unbounded.

Neuter: remove "PUT" from ``_WRITE_METHODS`` -> the PUT routes are reported
and this test goes RED.
"""

from __future__ import annotations

from audittrace.memory_body_limit_middleware import _PREFIXES, _WRITE_METHODS
from tests.memory_truncation_support import memory_client

_HTTP_VERBS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def test_every_memory_body_route_uses_a_bounded_verb() -> None:
    with memory_client() as client:
        paths = client.app.openapi()["paths"]
    body_ops = {
        (verb.upper(), path)
        for path, item in paths.items()
        if path.startswith(_PREFIXES)
        for verb, op in item.items()
        if verb in _HTTP_VERBS and "requestBody" in op
    }
    # non-vacuous: the memory write surface has POST and PUT body routes
    assert {verb for verb, _ in body_ops} >= {"POST", "PUT"}
    assert len(body_ops) >= 8
    unbounded = sorted(op for op in body_ops if op[0] not in _WRITE_METHODS)
    assert unbounded == []
