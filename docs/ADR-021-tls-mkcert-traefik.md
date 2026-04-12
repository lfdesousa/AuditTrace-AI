# ADR-021: TLS with mkcert + Traefik

**Status:** Accepted
**Date:** 2026-04-10
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-019 (ChromaDB server), ADR-020 (PostgreSQL)

## Context

Phase 0 ran over plain HTTP on localhost. For a production-grade sovereign AI
stack, all external-facing traffic must be encrypted. Internal service-to-service
traffic (memory-server to PostgreSQL, ChromaDB) stays on the Docker network
and does not traverse TLS in Phase 1 — Istio mTLS handles that in Phase 4.

## Decision

Use **Traefik v3** as reverse proxy with **mkcert** self-signed certificates
for development and LAN deployment.

### Architecture

```
Client (HTTPS :443)
    |
    v
Traefik (TLS termination)
    |
    v (plain HTTP, Docker network)
memory-server :8765
```

### Certificate Generation

```bash
./certs/generate-certs.sh
# Uses mkcert to generate:
#   certs/sovereign.pem
#   certs/sovereign-key.pem
# Trusted by: localhost, 127.0.0.1, ::1, sovereign-ai.local
```

### Traefik Configuration

- **Static config** (`traefik/traefik.yml`): HTTPS entrypoint on :443, Docker provider, dashboard on :8080
- **Dynamic config** (`traefik/dynamic.yml`): TLS certificate paths
- **Docker labels** on memory-server: route all traffic via HTTPS

### Production Path

For production deployment, replace mkcert with Let's Encrypt via Traefik's
ACME resolver. The Traefik configuration supports this with a single
`certResolver` change — no application code changes needed.

## Consequences

### Positive
- All external traffic encrypted
- Browser-trusted certificates on localhost (mkcert)
- Traefik dashboard for routing visibility (:8080)
- Clean upgrade path to Let's Encrypt for production

### Negative
- mkcert must be installed on the developer's machine
- Self-signed certs require `-k` flag with curl (or CA trust)

### Neutral
- Internal Docker network traffic remains plain HTTP (acceptable for Phase 1)
- Phase 4 (Istio) adds mTLS for internal traffic automatically
