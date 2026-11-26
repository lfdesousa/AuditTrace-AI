"""CONSOLE-AUDIT-EVIDENCE WU-C (ADDENDUM-A1 section 2, A2 section 3, G19): the
chart renders ``AUDITTRACE_EVIDENCE_SERVICE_READERS`` into the memory server, and
it equals what the chart actually grants.

The evidence view tells the user which database roles can read the audit row
(``visibility.answer.service_readers``). That list must be derived, not typed:
this guard equates, per chart state, (1) the roles the RENDERED SQL grants
``SELECT ON TABLE public.interactions`` to, (2) the rendered env value, and (3)
the frozen list ``['audittrace_summariser']``.

Grid: ``manageRole=true`` -> the summariser role (SQL ConfigMap renders);
``manageRole=false`` -> no SQL ConfigMap, env ``operator-managed``;
``postgresql.enabled=false`` (external DB) -> same as false.

Neuters (each RED, recorded in the build record): add ``GRANT SELECT ON TABLE
public.interactions TO audittrace_acl_lookup`` to the SQL (A1's); render the role
name unconditionally (A2's grid neuter); drop the env var; hardcode the literal.
"""

from __future__ import annotations

import re

import pytest

from tests.test_summariser_role_chart import _find, _render

ENV = "AUDITTRACE_EVIDENCE_SERVICE_READERS"
FROZEN = ["audittrace_summariser"]
OPERATOR_MANAGED = "operator-managed"
_BASE = [
    "--set",
    "secrets.summariser.password=dummy-pw-for-render",
    "--set",
    "secrets.minio.secretKey=dummy-minio-secret-for-render",
    "--set",
    "secrets.minio.kmsKey=dummy-minio-kms-for-render",
    "--set",
    "externalLLM.host=llm.test.invalid",
    "--set",
    "observability.external.langfuseHost=langfuse.test.invalid",
    "--set",
    "observability.external.tempoHost=tempo.test.invalid",
    "--set",
    "observability.external.lokiHost=loki.test.invalid",
]
_GRANT = re.compile(
    r"GRANT\s+[^;]*\bSELECT\b[^;]*\bON\s+TABLE\s+public\.interactions\s+TO\s+(\w+)\s*;",
    re.I,
)

GRID = {
    "manage-true": ([], FROZEN),
    "manage-false": (["--set", "memoryServer.summariser.manageRole=false"], None),
    "external-db": (["--set", "postgresql.enabled=false"], None),
}


def _env(docs: list[dict]) -> str:
    dep = _find(docs, "Deployment", "audittrace-memory-server")
    assert dep is not None, "the memory-server Deployment must render"
    envs = {
        e["name"]: e.get("value")
        for c in dep["spec"]["template"]["spec"]["containers"]
        for e in c.get("env", [])
    }
    assert ENV in envs, f"{ENV} is not rendered into the memory server"
    value = envs[ENV]
    assert isinstance(value, str)
    return value


def _granted_roles(docs: list[dict]) -> list[str]:
    """Roles the rendered chart grants SELECT on public.interactions to."""
    roles: set[str] = set()
    for doc in docs:
        if doc.get("kind") != "ConfigMap":
            continue
        for text in (doc.get("data") or {}).values():
            roles.update(m.group(1) for m in _GRANT.finditer(str(text)))
    return sorted(roles)


@pytest.mark.parametrize("case", sorted(GRID))
def test_service_readers_equals_chart_grants(case: str) -> None:
    overrides, expected_roles = GRID[case]
    docs = _render(_BASE + overrides)
    value = _env(docs)
    sql_cm = _find(docs, "ConfigMap", "audittrace-ensure-summariser-sql")
    if expected_roles is None:
        assert sql_cm is None, "the SQL ConfigMap must not render in this state"
        assert _granted_roles(docs) == []
        assert value == OPERATOR_MANAGED
    else:
        assert sql_cm is not None
        assert _granted_roles(docs) == expected_roles == FROZEN
        assert value.split(",") == expected_roles


def test_the_rendered_name_follows_the_role_name_value() -> None:
    docs = _render(_BASE + ["--set", "memoryServer.summariser.roleName=other_role"])
    assert _env(docs) == "other_role"


def test_the_env_is_rendered_into_exactly_one_container_once() -> None:
    dep = _find(_render(_BASE), "Deployment", "audittrace-memory-server")
    assert dep is not None
    names = [
        e["name"]
        for c in dep["spec"]["template"]["spec"]["containers"]
        for e in c.get("env", [])
    ]
    assert names.count(ENV) == 1
