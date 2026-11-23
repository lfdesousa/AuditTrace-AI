"""The ``visibility`` and ``sources`` parts of the evidence document.

"Who can see this" reports the MECHANISM, never a promise: the API reader of an answer is
its owner (row-level security on ``interactions``, no API scope lifts it); one provisioned
service role reads the table across users by design; operators with database access read
everything (ADDENDUM-A1 section 2). The service roles are served from configuration the
chart renders, never typed here.
"""

from __future__ import annotations

from typing import Any

OPERATOR_MANAGED = "operator-managed"
SERVICE_PURPOSE = "session summarisation"
_GRANT_BASIS = "BYPASSRLS + GRANT SELECT ON public.interactions"


def service_readers(raw: str) -> list[dict[str, str]]:
    """Parse the comma-separated role list; blank or ``operator-managed`` -> unverified."""
    roles = [part.strip() for part in raw.split(",") if part.strip()]
    if not roles or roles == [OPERATOR_MANAGED]:
        return [
            {
                "role": OPERATOR_MANAGED,
                "basis": "unverified",
                "purpose": SERVICE_PURPOSE,
            }
        ]
    return [
        {"role": role, "purpose": SERVICE_PURPOSE, "basis": _GRANT_BASIS}
        for role in roles
    ]


def answer_visibility(*, owner_is_caller: bool, readers_raw: str) -> dict[str, Any]:
    return {
        "api_readers": ["owner"],
        "owner_is_caller": owner_is_caller,
        "basis": "postgres-rls:tenant_isolation_interactions",
        "service_readers": service_readers(readers_raw),
        "operator_access": "database administrators",
    }


def sources_block(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """``recorded`` iff at least one tool call has a non-null ``recall_matches``
    (``null`` = not captured, ``[]`` = captured with zero matches), else
    ``not_recorded`` with no items: never an empty list that reads as "checked, found
    nothing"."""
    captured = [tc for tc in tool_calls if tc["recall_matches"] is not None]
    if not captured:
        return {"status": "not_recorded", "items": []}
    items = [
        {
            "source_ref": match.get("source_ref"),
            "id": match.get("id"),
            "sha256": match.get("sha256"),
            "collection": match.get("collection"),
            "tool_name": tc["tool_name"],
            "visibility": {
                "tier": match.get("tier"),
                "owner_is_caller": match.get("owner_is_caller"),
                "basis": "recall-metadata:tier",
            },
        }
        for tc in captured
        for match in tc["recall_matches"]
    ]
    return {"status": "recorded", "items": items}
