"""AuditTrace LibreChat BFF (Backend-for-Frontend) — M3-WU-2.

A dedicated sidecar (ADR-042 §5 Option A) that sits between the LibreChat
console and the AuditTrace orchestrator's ``/v1`` surface. It is a
*distinct deployable*, not a code path inside ``src/audittrace`` — see
``bff/README.md`` for the placement rationale.

On every request the BFF:

1. Receives the per-user token LibreChat forwards (``Authorization: Bearer``).
2. Validates it against the AuditTrace Keycloak realm's JWKS (signature,
   issuer, expiry) — never trusting an unverified token.
3. RFC 8693 token-exchanges it (confidential client
   ``audittrace-librechat-bff``) into a fresh access token carrying
   ``aud=audittrace-server`` + ``audittrace:query``, minted for the SAME
   subject (``sub``) as the inbound token.
4. Proxies ``POST /v1/chat/completions`` to the orchestrator with the
   minted token, streaming the response back byte-identical (SSE
   preserved).

This makes the LibreChat token-forwarding ambiguity (access vs id_token,
audience) irrelevant to the orchestrator: whatever LibreChat forwards, the
BFF deterministically re-mints the token shape ``/v1`` requires.
"""

from __future__ import annotations
