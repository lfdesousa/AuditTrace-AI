"""Why an inbound token was not accepted (BFF-CHAT-ROUTE-ERRORS)."""

from __future__ import annotations

import enum


class InboundTokenFailure(enum.Enum):
    """The three classes a caller-facing answer distinguishes."""

    MISSING = "missing"  # no bearer credentials in the request
    INVALID = "invalid"  # credentials present, not a valid token of this realm
    UNVERIFIABLE = "unverifiable"  # the realm's keys could not be fetched or used
