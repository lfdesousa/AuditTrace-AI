"""Why an RFC 8693 token exchange failed (BFF-CHAT-ROUTE-ERRORS)."""

from __future__ import annotations

import enum


class ExchangeFailure(enum.Enum):
    """The four classes a caller-facing answer distinguishes."""

    SUBJECT_REFUSED = "subject_refused"  # 400 invalid_token: the caller's token
    REFUSED = "refused"  # any other 4xx: the BFF's own configuration
    UNAVAILABLE = "unavailable"  # transport failure, timeout, 5xx or unusable keys
    INVALID = "invalid"  # 200, but the token is unusable or breaks an invariant
