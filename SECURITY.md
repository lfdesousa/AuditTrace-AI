# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly.

**Do not open a public GitHub issue.**

Instead, email: **ldesousapro@allaboutdata.eu**

Include:
- Description of the vulnerability
- Steps to reproduce
- Impact assessment
- Suggested fix (if any)

You will receive an acknowledgement within 48 hours and a detailed response within 5 business days.

## Security Architecture

- All external traffic encrypted via TLS (Traefik + mkcert/Let's Encrypt)
- JWT authentication via Keycloak with private_key_jwt (no shared secrets)
- PostgreSQL with least-privilege roles
- ChromaDB with token-based authentication
- Database services on Docker internal network only
- No credentials in code — environment variables and Docker secrets only

See [ADR-022](docs/ADR-022-keycloak-realm.md) and [ADR-023](docs/ADR-023-jwt-validation-jwks-caching.md) for details.
