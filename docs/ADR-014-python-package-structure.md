# ADR-014: Python Package Structure for Memory Server

**Status**: Accepted
**Date**: 2026-04-09
**Authors**: Luis Filipe de Sousa
**Phase**: Phase 0

---

## Context

The memory server currently exists as a monolithic script with mixed concerns:
- Server logic (`langchain_server.py`)
- Backend abstraction (`langchain_backend.py`)
- Chain orchestration (`langchain_chain.py`)
- Memory management (`memory.py`)
- Tracing (`_tracing.py`)

This structure is not:
- Testable in isolation
- Publishable as a Python package
- Maintainable for team collaboration
- Compliant with Julien Danjou's Python engineering best practices

## Decision

Adopt a production-grade Python package structure following Julien Danjou's patterns:

```
audittrace-server/
├── pyproject.toml              # Modern packaging (PEP 621)
├── Dockerfile                  # Multi-stage, non-root user
├── .env.example                # All configuration documented
├── src/
│   └── audittrace/
│       ├── __init__.py
│       ├── server.py           # FastAPI app factory
│       ├── config.py           # Pydantic settings (12-factor)
│       ├── models.py           # Pydantic schemas (all endpoints)
│       ├── auth.py             # JWT middleware (Phase 2)
│       ├── tracing.py          # Langfuse + OpenTelemetry
│       ├── backend.py          # ChromaDB/PostgreSQL abstraction
│       ├── chain.py            # LangChain orchestration
│       └── routes/
│           ├── __init__.py
│           ├── chat.py         # /v1/chat/completions
│           ├── context.py      # /context
│           ├── audit.py        # /interactions
│           ├── session.py      # /session/save
│           └── health.py       # /health /metrics
├── tests/
│   ├── conftest.py             # Test fixtures
│   ├── test_config.py
│   ├── test_models.py
│   ├── test_routes.py
│   ├── test_auth.py            # Phase 2
│   └── test_backend.py         # Phase 1
├── docs/
│   └── ADR-001 through ADR-021
├── scripts/
│   └── setup.sh                # First-time setup
├── examples/
│   └── agent-configs/          # Keycloak client examples
└── README.md
```

### Key Design Decisions

1. **`pyproject.toml`** (PEP 621)
   - No `setup.py` - modern standard
   - All metadata in one place
   - Built-in support for `setuptools`, `hatch`, `poetry`

2. **`src/` layout**
   - Prevents accidental imports from current directory
   - Matches pytest best practices
   - Enables `pip install -e .` for development

3. **Pydantic settings** (`config.py`)
   - 12-factor architecture: environment variables
   - `AUDITTRACE_` prefix for all config
   - `lru_cache` on `get_settings()` for singleton pattern
   - Zero hardcoded values

4. **Pydantic models** (`models.py`)
   - All request/response schemas consolidated
   - Type hints for OpenAPI auto-generation
   - Validation at boundaries

5. **Route modularization** (`routes/`)
   - One file per logical endpoint group
   - Easy to test in isolation
   - Enables parallel development

6. **Docker multi-stage build**
   - Stage 1: Build dependencies (pip install)
   - Stage 2: Runtime (slim image, non-root user)
   - Security: no root user in production image
   - Health check for Kubernetes

7. **Testing strategy**
   - `pytest` with `pytest-asyncio`
   - `TestClient` for FastAPI integration tests
   - `conftest.py` for shared fixtures
   - Coverage requirements in CI

## Consequences

### Positive
- ✅ Publishable as `pip install audittrace-server`
- ✅ Testable components in isolation
- ✅ Clear separation of concerns
- ✅ Compliant with Julien Danjou Python engineering standards
- ✅ Ready for CI/CD (GitHub Actions)
- ✅ Security-first (non-root Docker user)
- ✅ 12-factor compliant (environment variables)

### Negative
- 📝 More initial setup work (Phase 0: 1 weekend)
- 📝 Need to refactor existing files into new structure
- 📝 Requires migration from SQLite to PostgreSQL for Phase 1

### Neutral
- 🔄 Import paths change from `langchain_server` to `audittrace.server`
- 🔄 Configuration now uses `AUDITTRACE_` prefix instead of bare variables

## Migration Plan

1. Create directory structure
2. Move and refactor `config` logic to `config.py`
3. Consolidate Pydantic models to `models.py`
4. Split routes into `routes/` modules
5. Create `server.py` app factory
6. Write tests for each component
7. Update Dockerfile
8. Document in README

## References

- Julien Danjou, *Python Engineering* (O'Reilly)
- Julien Danjou, *Scaling Python* (O'Reilly)
- FastAPI documentation: https://fastapi.tiangolo.com/
- Pydantic documentation: https://docs.pydantic.dev/
- PEP 621: https://peps.python.org/pep-0621/
- 12-Factor App: https://12factor.net/

---

**Next**: Phase 0 implementation begins with `pyproject.toml` and `config.py`
