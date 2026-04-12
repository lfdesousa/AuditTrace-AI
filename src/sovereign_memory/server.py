"""FastAPI application factory for sovereign-memory-server."""

from contextlib import asynccontextmanager
from logging import getLogger

from fastapi import FastAPI

from sovereign_memory import telemetry
from sovereign_memory.config import get_settings
from sovereign_memory.db.rls import install_rls_listener
from sovereign_memory.dependencies import register_default_dependencies
from sovereign_memory.logging_config import setup_logging
from sovereign_memory.routes import audit, chat, context, health, session

logger = getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler - startup and shutdown."""
    settings = get_settings()
    setup_logging(level=settings.log_level, structured=False)

    telemetry.init_telemetry(
        service_name=settings.otel_service_name,
        otlp_endpoint=settings.otlp_endpoint,
        tracing_enabled=settings.tracing_enabled,
        metrics_enabled=settings.metrics_enabled,
    )

    # Auto-instrument FastAPI request handling (ADR-014.4).
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as e:  # pragma: no cover
        logger.warning("FastAPI OTel instrumentation failed: %s", e)

    # Register DI container with all services (ADR-020)
    register_default_dependencies(settings)

    # Install the Phase 4 RLS after_begin listener so every DB
    # transaction pushes app.current_user_id into Postgres for RLS
    # evaluation. Idempotent and a no-op on SQLite (tests).
    install_rls_listener()

    logger.info("Starting sovereign-memory-server v0.2.0")
    logger.info("LLM URL: %s", settings.llama_url)
    logger.info("ChromaDB URL: %s", settings.chroma_url)
    logger.info("Auth enabled: %s", settings.auth_enabled)
    logger.info("Log level: %s", settings.log_level)
    logger.info(
        "OTel: service=%s export=%s",
        settings.otel_service_name,
        settings.otlp_endpoint or "disabled",
    )

    yield

    logger.info("Shutting down sovereign-memory-server")
    telemetry.shutdown()


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI(
        title="Sovereign Memory Server",
        description=(
            "Production-grade sovereign AI memory server with 4-tier memory "
            "and OAuth2 authentication"
        ),
        version="0.2.0",
        lifespan=lifespan,
    )

    app.include_router(chat.router, prefix="/v1", tags=["chat"])
    app.include_router(context.router, tags=["context"])
    app.include_router(audit.router, tags=["audit"])
    app.include_router(session.router, prefix="/session", tags=["session"])
    app.include_router(health.router, tags=["health"])

    return app


app = create_app()


def main():
    """Entry point for CLI execution."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "sovereign_memory.server:app",
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
