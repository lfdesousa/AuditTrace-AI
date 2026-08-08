"""Shared front-door URL resolver for the deploy tooling (SPEC #401).

Precedence: explicit argument > ``AUDITTRACE_FRONT_DOOR`` env var (read FRESH
at call time, NOT at import — environment-dynamic) > hardcoded fallback.

Single source of truth: ``memory.py``, ``verify.py``, and any future deploy
tool must import :func:`resolve_front_door` instead of hardcoding the URL.

This closes the data-integrity bug where ``scripts/deploy/memory.py``
hardcoded ``DEFAULT_FRONT_DOOR`` to the cloud URL, causing the fleet's OWN
verdict-logging to fail on the laptop (log_deploy_record could not reach the
local memory server) — lost data, unacceptable.
"""

from __future__ import annotations

import os

FRONT_DOOR_ENV_VAR = "AUDITTRACE_FRONT_DOOR"
DEFAULT_FRONT_DOOR = "https://audittrace.allaboutdata.eu"


def resolve_front_door(explicit: str | None = None) -> str:
    """Resolve the front-door base URL with the standard precedence chain.

    Precedence (highest to lowest):

    1. **explicit argument** — if a non-empty string is passed, it is used
       verbatim (the caller already validated it).
    2. **``AUDITTRACE_FRONT_DOOR`` env var** — read FRESH at call time via
       :func:`os.environ.get` (NOT cached at import), so this is truly
       environment-dynamic: changing the env var between calls is respected.
    3. **hardcoded fallback** — :data:`DEFAULT_FRONT_DOOR` for backward
       compatibility with any caller that has no env set and no explicit arg.

    The caller that uses the result is responsible for
    :func:`scripts.deploy.memory._normalize_front_door` validation (scheme +
    host check). This resolver returns the raw URL.
    """
    if explicit:
        return explicit
    raw = os.environ.get(FRONT_DOOR_ENV_VAR, "").strip()
    return raw or DEFAULT_FRONT_DOOR
