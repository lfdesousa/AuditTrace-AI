---
title: "infra: add docker-compose.dev.yml with bind-mounted src + uvicorn --reload"
labels: ["enhancement", "developer-experience", "infrastructure"]
priority: P3
---

## Context

`docker-compose.yml` does not bind-mount `src/` into the `memory-server`
container. Every code change requires:

```bash
docker compose up -d --build memory-server
```

…which takes ~20-30 seconds for the layer rebuild + image swap. This adds
~30s of latency to every iteration during active development. On 2026-04-11
during the ADR-024 work, this happened five times in one hour.

## Fix sketch

Add a `docker-compose.dev.yml` overlay that:

1. Bind-mounts `./src:/app/src:ro`
2. Bind-mounts `./pyproject.toml:/app/pyproject.toml:ro` (in case deps change
   — though dep changes still need a rebuild)
3. Overrides the entrypoint to launch uvicorn with `--reload --reload-dir /app/src`
4. Sets `SOVEREIGN_LOG_LEVEL=DEBUG`

Usage:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

Production runs of `docker compose up -d` (no override) keep the baked image.

## Constraints

- Must NOT change the production image baking behaviour
- Must work with the existing entrypoint script's alembic migration step
- Must coexist with the existing `volumes:` for `memory/episodic` and
  `memory/procedural` (which are read-only mounts already)

## Acceptance criteria

- New `docker-compose.dev.yml` file
- README updated with the dev-loop one-liner
- Editing a `.py` file under `src/` causes a uvicorn reload within 2 seconds
  without a rebuild
- Production `docker compose up -d` is unchanged
