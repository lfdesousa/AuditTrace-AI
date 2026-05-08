"""Admin routes — operator-facing maintenance endpoints (ADR-052).

The first endpoint here is ``POST /system/trust-store/refresh``: walks
the configured ``TrustStoreBuilder`` (default
``EuLotlTrustStoreBuilder``) and persists the result via the configured
``TrustStoreProvider`` (default ``S3TrustStoreProvider`` → MinIO at
``memory-shared/trust-store/eu-lotl-bundle.pem``). Synchronous from
the operator's perspective — the response carries the resulting
``TrustStoreMetadata`` (sha256, builder_id, built_at, cert_count,
source_url) so the operator can confirm the refresh actually fetched
fresh roots.

Auth: scope ``audittrace:admin`` (mirrors the existing per-layer
admin checks in ``routes/memory.py:200-206``). Helm post-install hook
Job uses a ServiceAccount-issued JWT with this scope; human operators
mint one via the Device Flow.

Future admin endpoints (status readout, cache inspection,
manual force-rebuild via specified Builder) join here behind the
same router.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi import status as http_status

from audittrace.auth import require_user, validate_jwt
from audittrace.dependencies import (
    get_trust_store_builder,
    get_trust_store_provider,
)
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.routes.memory import _invalidate_validation_context
from audittrace.services.trust_store import (
    TrustStoreBuilder,
    TrustStoreBuilderUnavailableError,
    TrustStoreProvider,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/trust-store/refresh",
    summary="Refresh PAdES trust store",
    response_description="TrustStoreMetadata of the persisted bundle",
)
@log_call(logger=logger)
async def refresh_trust_store(
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:admin"]),
    user: UserContext = Depends(require_user),
    provider: TrustStoreProvider = Depends(get_trust_store_provider),
    builder: TrustStoreBuilder = Depends(get_trust_store_builder),
) -> dict[str, Any]:
    """Walk the configured Builder, persist via the configured Provider,
    invalidate the in-process ValidationContext singleton, return the
    new bundle's metadata.

    Status codes:

    * 200 — bundle refreshed; response body = TrustStoreMetadata.
    * 401 — missing or invalid JWT.
    * 403 — JWT lacks ``audittrace:admin``.
    * 502 — Builder unavailable (pyhanko[etsi] missing, EU LOTL
      unreachable, XAdES verification failed). The previously-stored
      bundle remains in place; subsequent signature checks continue
      to use it. Operator can retry once upstream conditions clear.
    * 500 — Provider write failed (MinIO down, permission issue).
      Investigate and retry.
    """
    # Defence-in-depth: validate_jwt's scope check is the gate, but
    # mirror the existing per-route ``user.is_admin`` style guard from
    # ``routes/memory.py:200-206`` so the failure mode is identical
    # to other admin-scoped routes if the JWT shape ever drifts.
    if not (user.is_admin or "audittrace:admin" in user.scopes):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Required scope: audittrace:admin",
        )

    logger.info(
        "trust-store refresh requested by user=%s builder_id=%s",
        user.user_id,
        builder.builder_id,
    )

    try:
        bundle = await builder.build()
    except TrustStoreBuilderUnavailableError as exc:
        # Typed unavailable — Builder cannot run in this environment
        # (pyhanko[etsi] missing, EU LOTL unreachable, etc.).
        # Surface as 502 with the cause string for the operator to
        # see in the response body + Loki.
        logger.warning("trust-store refresh — Builder unavailable: %s", exc)
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "trust_store_build_failed",
                "builder_id": builder.builder_id,
                "cause": str(exc),
            },
        ) from exc

    try:
        provider.store(bundle)
    except Exception as exc:
        logger.error(
            "trust-store refresh — Provider.store() failed: %r",
            exc,
        )
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "trust_store_persist_failed",
                "cause": repr(exc),
            },
        ) from exc

    # Invalidate the in-process ValidationContext singleton so the
    # next signature check rebuilds against the new PEM bytes. The
    # cache-key check in _get_validation_context (which keys on the
    # Provider metadata sha256) would catch this on its own, but
    # explicit invalidation skips a slow-path round-trip.
    _invalidate_validation_context()

    logger.info(
        "trust-store refresh — success builder_id=%s sha256=%s cert_count=%d",
        bundle.metadata.builder_id,
        bundle.metadata.sha256[:16] + "…",
        bundle.metadata.cert_count,
    )

    return bundle.metadata.to_dict()


@router.get(
    "/trust-store",
    summary="Read PAdES trust-store metadata",
    response_description="TrustStoreMetadata of the currently-stored bundle",
)
@log_call(logger=logger)
async def get_trust_store_metadata(
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:admin"]),
    user: UserContext = Depends(require_user),
    provider: TrustStoreProvider = Depends(get_trust_store_provider),
) -> dict[str, Any]:
    """Return the metadata of the currently-stored bundle, or 404 if
    no bundle has been provisioned yet.

    Cheaper than reading the full bundle — pulls only the metadata
    sidecar object. Useful for health-style readouts and the Helm
    hook's pre-flight idempotency check.
    """
    if not (user.is_admin or "audittrace:admin" in user.scopes):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Required scope: audittrace:admin",
        )
    metadata = provider.metadata()
    if metadata is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={
                "error": "trust_store_not_provisioned",
                "hint": "POST /system/trust-store/refresh to populate",
            },
        )
    return metadata.to_dict()
