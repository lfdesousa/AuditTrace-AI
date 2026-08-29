"""Shared fixtures for the LibreChat BFF test suite.

Sets ``AUDITTRACE_BFF_ENV=test`` (mirrors the root ``tests/conftest.py``
pattern for ``audittrace.config``) BEFORE anything imports ``bff.config``,
so ``bff.config._ENV_FILE`` never tries to load a developer's local
``.env``. Also provides the RSA test key pair + JWT-signing helper every
BFF test file uses to build inbound/minted tokens — same technique as
``tests/test_auth.py`` (no real Keycloak required).
"""

from __future__ import annotations

import os
import time

os.environ["AUDITTRACE_BFF_ENV"] = "test"
os.environ.setdefault("AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET", "test-secret-never-real")

import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from jose import jwt  # noqa: E402

from bff import auth as bff_auth  # noqa: E402
from bff.config import get_settings  # noqa: E402

# ── Test RSA key pair (BFF's own realm; independent of test_auth.py's) ──────

_test_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_test_public_key = _test_private_key.public_key()

TEST_PRIVATE_PEM = _test_private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

TEST_PUBLIC_PEM = _test_public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()

TEST_ISSUER = "http://keycloak:8080/realms/audittrace"


def make_token(
    sub: str = "alice",
    issuer: str = TEST_ISSUER,
    exp_offset: int = 3600,
    extra_claims: dict | None = None,
    private_pem: str = TEST_PRIVATE_PEM,
) -> str:
    """Sign a test JWT. No ``aud`` claim by default — the BFF's inbound-
    token validation deliberately does not enforce one (see
    ``bff/auth.py``); tests that need a specific audience pass it via
    ``extra_claims``."""
    now = int(time.time())
    claims = {
        "iss": issuer,
        "sub": sub,
        "iat": now,
        "exp": now + exp_offset,
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, private_pem, algorithm="RS256")


@pytest.fixture(autouse=True)
def _reset_bff_state():
    """Isolate every test from cached Settings + the JWKS cache."""
    get_settings.cache_clear()
    bff_auth.reset_jwks_state_for_tests()
    yield
    get_settings.cache_clear()
    bff_auth.reset_jwks_state_for_tests()
