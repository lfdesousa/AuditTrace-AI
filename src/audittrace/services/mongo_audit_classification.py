"""LibreChat MongoDB collection classification — ratified, version-controlled (N-4).

Part of **WU-A1** of the Mongo-audit route
(``~/work/audittrace-private/specs/2026-09-25-SPEC-mongo-audit-sanctioned-read-route-CONSOLIDATED-v5.md``,
``sha256:b47de202681f36114e5568531e8ed11d320dbc63e3c4a976292d34663035ff72``). Pure data + one pure
function — no HTTP, no ``pymongo``, no config.

**N-4** (ADDENDUM N): the classification is ratified *before* the audit query ever runs, so a
mis-typed or missing mapping key surfaces as ``UNCLASSIFIED`` → ``INCOMPLETE``, never as a silent
pass. **This mapping is the ratified table itself (ADDENDUM S, operator-ratified 2026-09-25,
carried into the spec's §6 with per-row code citations) — it is a ruling, not a judgement call. A
builder does not re-derive or "improve" it.**

Counts: **USER = 34 · OPERATIONAL = 13 · 47 named collections + the pinned `system.*` prefix
rule.** Mongoose-registered: 42 (USER 31 + OPERATIONAL 11). Non-Mongoose: 5 (USER 3 +
OPERATIONAL 2). Re-derived from the fork's wire (``mongoose.pluralize()`` output) at spec
authoring time — see the spec's §1 #35-36 and §6 for the per-row citations into
``~/work/LibreChat`` @ ``768de61``.

Rule of construction (spec §6): **USER** = authored by, or a record of, an end user's use of the
product (content, per-user credentials, per-user state); **OPERATIONAL** = identity/auth
plumbing, seeded/admin config, caches, Mongo's own bookkeeping. When in doubt, USER.

Reclassification is a PR that moves this mapping, ``CLASSIFICATION_VERSION`` and the spec's
successor together — never a report-time decision (spec §6).
"""

from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import Literal

Classification = Literal["USER", "OPERATIONAL", "UNCLASSIFIED"]

# §6.1 USER — must be zero (34). Comments cite the spec's per-row source; the ratified class
# itself carries no further rationale here — see spec §6.1 for the user-binding evidence.
_USER: dict[str, Classification] = {
    "conversations": "USER",  # models/convo.ts
    "messages": "USER",  # models/message.ts
    "presets": "USER",  # models/preset.ts
    "prompts": "USER",  # models/prompt.ts
    "promptgroups": "USER",  # models/promptGroup.ts
    "chatprojects": "USER",  # models/chatProject.ts:12 (explicit name)
    "files": "USER",  # models/file.ts (metadata; bodies in S3)
    "agents": "USER",  # models/agent.ts — NO shim (shim 8, gated on ACL epic, D18=B)
    "conversationtags": "USER",  # models/conversationTag.ts
    "toolfavorites": "USER",  # models/favorite.ts
    "memoryentries": "USER",  # models/memory.ts (panel via BFF, D2-2)
    "sharedlinks": "USER",  # models/sharedLink.ts + shared-links/service.ts:114
    "pluginauths": "USER",  # models/pluginAuth.ts — per-user credentials
    "keys": "USER",  # models/key.ts — per-user API keys
    "actions": "USER",  # models/action.ts
    "assistants": "USER",  # models/assistant.ts
    "toolcalls": "USER",  # models/toolCall.ts — conversation content
    "schedules": "USER",  # models/schedule.ts
    "scheduleruns": "USER",  # models/schedule.ts (2nd registration)
    "transactions": "USER",  # models/transaction.ts — per-user usage records
    "balances": "USER",  # models/balance.ts
    "agentapikeys": "USER",  # models/agentApiKey.ts — per-user secrets
    "mcpservers": "USER",  # models/mcpServer.ts — may hold credentials
    "aclentries": "USER",  # models/aclEntry.ts — user-authored sharing state (ACL epic)
    "skills": "USER",  # models/skill.ts
    "skillfiles": "USER",  # models/skillFile.ts
    "skillsynccredentials": "USER",  # models/skillSyncCredential.ts — per-user sync creds
    "skillsyncstatuses": "USER",  # models/skillSyncStatus.ts
    "agenttriggerdeliveries": "USER",  # models/triggerDelivery.ts
    "agenttriggerlanesequences": "USER",  # models/triggerLaneSequence.ts:11-14 (42nd model)
    "tokens": "USER",  # models/token.ts — per-user MCP OAuth access + refresh tokens (S re-ruled)
    "agent_checkpoints": "USER",  # non-Mongoose, agents/checkpointer.ts:547
    "agent_checkpoint_writes": "USER",  # non-Mongoose, agents/checkpointer.ts:548
    "projects": "USER",  # non-Mongoose legacy, agents/migration.ts:75, prompts/migration.ts:72
}

# §6.2 OPERATIONAL — may be non-zero (13).
_OPERATIONAL: dict[str, Classification] = {
    "users": "OPERATIONAL",  # models/user.ts — the positive control (OIDC-linked identity)
    "sessions": "OPERATIONAL",  # models/session.ts — refresh sessions, ruled auth plumbing
    "roles": "OPERATIONAL",  # models/role.ts — seeded
    "accessroles": "OPERATIONAL",  # models/accessRole.ts — seeded permission-role definitions
    "systemgrants": "OPERATIONAL",  # models/systemGrant.ts — system-level authorization
    "groups": "OPERATIONAL",  # models/group.ts — org structure (trip-wire, spec §6.2 #6)
    "banners": "OPERATIONAL",  # models/banner.ts — admin content
    "configs": "OPERATIONAL",  # models/config.ts — application configuration
    "auditlogs": "OPERATIONAL",  # models/auditLog.ts — the fork's own admin audit log
    "agentcategories": "OPERATIONAL",  # models/agentCategory.ts — seeded
    "agenttriggeruserpurges": "OPERATIONAL",  # models/triggerUserPurge.ts — deletion tombstones
    "logs": "OPERATIONAL",  # non-Mongoose, cache/keyvMongo.ts:274-276 — keyv cache store
    "__transaction_test__": "OPERATIONAL",  # non-Mongoose, utils/transactions.ts:19
}

# Exactly the 47 named collections of spec §6 (34 USER + 13 OPERATIONAL). `system.*` is handled
# separately by `classify()` as the single prefix rule (§5.4) — it is never a key in this mapping.
CLASSIFICATION: MappingProxyType[str, Classification] = MappingProxyType(
    {**_USER, **_OPERATIONAL}
)


def _canonical_json(mapping: MappingProxyType[str, Classification]) -> bytes:
    """Deterministic serialisation of the mapping for hashing.

    Sorted keys + fixed separators so the version is stable across dict
    insertion order / whitespace — the only two things Python's ``json``
    module does not otherwise guarantee to be constant across processes.
    """
    return json.dumps(dict(sorted(mapping.items())), separators=(",", ":")).encode(
        "utf-8"
    )


CLASSIFICATION_VERSION: str = hashlib.sha256(
    _canonical_json(CLASSIFICATION)
).hexdigest()

# §5.4: `system.buckets.` is carved out of the `system.` prefix rule — MongoDB >= 5 stores
# time-series *data* there, not Mongo's own bookkeeping (spec §5.4, v2-A-5).
_OPERATIONAL_PREFIX = "system."
_UNCLASSIFIED_CARVE_OUT_PREFIX = "system.buckets."


def classify(name: str) -> Classification:
    """Classify one server-reported collection name (spec §5.4).

    Exact match against the ratified mapping wins. Otherwise, a name that
    starts with ``system.`` (Mongo's own namespace) but not
    ``system.buckets.`` (time-series *data*, carved out) is ``OPERATIONAL``.
    Everything else is ``UNCLASSIFIED`` — which the verdict algorithm turns
    into ``INCOMPLETE``, never a silent pass (N-4).
    """
    exact = CLASSIFICATION.get(name)
    if exact is not None:
        return exact
    if name.startswith(_OPERATIONAL_PREFIX) and not name.startswith(
        _UNCLASSIFIED_CARVE_OUT_PREFIX
    ):
        return "OPERATIONAL"
    return "UNCLASSIFIED"
