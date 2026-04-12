workspace "sovereign-memory-server" "4-Layer Memory Augmentation Proxy for Local LLMs" {

    !identifiers hierarchical
    !impliedRelationships true

    model {

        // Actors
        agent = person "Coding Agent" "OpenCode, Roo Code, Continue — any OpenAI-compatible client"
        architect = person "Luis Filipe" "Solutions Architect — configures memory layers and ADRs"

        // External systems
        llamaServer = softwareSystem "llama-server" "Local LLM inference (Qwen3.5-35B-A3B on ROCm)" {
            tags "External"
        }
        embedServer = softwareSystem "Embedding Server" "nomic-embed-text on CPU (:11436)" {
            tags "External"
        }
        langfuse = softwareSystem "Langfuse" "Observability — traces, spans, metrics (sibling stack)" {
            tags "External"
        }
        keycloak = softwareSystem "Keycloak" "Identity provider — owns users/roles/scopes; issues OIDC JWTs (ADR-022, DESIGN §15)" {
            tags "External"
        }

        // The system
        memoryServer = softwareSystem "sovereign-memory-server" "Transparent augmentation proxy with 4-layer memory + Keycloak-delegated identity" {

            api = container "FastAPI Application" "OpenAI-compatible API + /context endpoint" "Python / FastAPI / uvicorn" {

                chatRoute = component "Chat Route" "/v1/chat/completions — async raw-dict pass-through; branches on memory_mode={inject|tools} (ADR-024, ADR-025)" "FastAPI Router"
                contextRoute = component "Context Route" "/context — retrieve assembled context" "FastAPI Router"
                healthRoute = component "Health Route" "/health, /metrics" "FastAPI Router"

                // Identity layer (DESIGN §15) — replaces the Phase 0/1 PAT model
                requireUser = component "require_user" "FastAPI dependency: validates Keycloak JWT, returns typed UserContext (DESIGN §15)" "auth.py"
                tokenCache = component "TokenCache" "sha256(token) → UserContext, TTL eviction, Redis-backed (DESIGN §15.4)" "identity.py"
                requireScope = component "require_scope (legacy)" "JWT scope check returning raw payload dict (ADR-022, ADR-023)" "auth.py"

                // Memory layers (inject mode + shared by tools mode handlers)
                contextBuilder = component "ContextBuilderService" "Aggregates all 4 memory layers (inject mode) + build_ambient_context (tools mode)" "ABC + DefaultContextBuilder"
                episodicSvc = component "EpisodicService" "Layer 1 — ADR markdown loading" "ABC + FileEpisodicService"
                proceduralSvc = component "ProceduralService" "Layer 2 — SKILL file loading" "ABC + FileProceduralService"
                conversationalSvc = component "ConversationalService" "Layer 3 — PostgreSQL sessions (ADR-020)" "ABC + PostgresConversationalService"
                semanticSvc = component "SemanticService" "Layer 4 — ChromaDB vector search" "ABC + ChromaSemanticService"

                // Memory-as-tools (ADR-025) — dynamic registry + tool-call loop
                memoryToolRegistry = component "MemoryToolRegistry" "Decorator-based registry (@register_memory_tool). tools_visible_to(user) scope filter. invoke_tool cache-aware dispatch. Optional TOML overlay (ADR-025)" "tools/__init__.py"
                memoryHandlers = component "Memory Tool Handlers" "recall_decisions, recall_skills, recall_recent_sessions, recall_semantic — thin adapters to the 4 services, canonical {matches, total, truncated} result schema" "tools/memory_handlers.py"
                toolResultCache = component "ToolResultCache" "sha256(session|tool|args) → result dict, TTL eviction, Redis-backed, namespace sovereign:tool-result:, disjoint from TokenCache (ADR-025 §Decision.8)" "tools/cache.py"
                memoryToolLoop = component "Memory Tool-Call Loop" "Proxy-internal non-streaming round-trip: dispatch → llama → tool_calls → execute memory tools → repeat. Bounded by SOVEREIGN_MEMORY_TOOL_LOOP_MAX_ITERATIONS. Produces PendingToolCall audit records (ADR-025 §Decision.2)" "routes/_memory_tool_loop.py"

                // Plumbing
                diContainer = component "DependencyContainer" "DI — registers factories and services" "Python"
                pgFactory = component "PostgresFactory" "Connection pooling + session management (ADR-020)" "ABC + URLPostgresFactory"
                telemetry = component "Telemetry" "OTel spans + Langfuse SDK routing (ADR-024 §15.x)" "telemetry.py + logging_config.py"
            }

            postgresDb = container "PostgreSQL 16" "Audit trail (interactions, sessions, tool_calls) — user_id is Keycloak sub, no local users table" "PostgreSQL" {
                tags "Database"
            }
            chromaDb = container "ChromaDB Server" "Vector store — decisions, skills collections — token auth" "ChromaDB HTTP Server" {
                tags "Database"
            }
            redisCache = container "Redis 7" "Shared cache — sovereign:token:* for TokenCache (DESIGN §15.4) and sovereign:tool-result:* for ToolResultCache (ADR-025 §Decision.8). Disjoint key prefixes." "Redis" {
                tags "Database"
            }
            adrFiles = container "ADR Files" "Architecture Decision Records (ADR-*.md)" "Filesystem" {
                tags "Database"
            }
            skillFiles = container "SKILL Files" "Procedural skill documents (SKILL-*.md)" "Filesystem" {
                tags "Database"
            }
        }

        // Relationships — system level
        agent -> keycloak "OAuth2 device flow → JWT" "HTTPS/OIDC"
        agent -> memoryServer.api "POST /v1/chat/completions (Bearer JWT)" "HTTPS/JSON"
        architect -> memoryServer.adrFiles "Writes ADRs"
        architect -> memoryServer.skillFiles "Writes SKILL files"
        memoryServer.api -> llamaServer "Proxies augmented request (async, streaming)" "HTTP/SSE"
        memoryServer.api -> keycloak "Fetch JWKS public keys (cached 5 min)" "HTTP/JSON"
        memoryServer.api -> langfuse "Exports traces + per-trace updates via ingestion API (ADR-024)" "HTTP/OTLP"

        // Relationships — identity layer (DESIGN §15)
        memoryServer.api.chatRoute -> memoryServer.api.requireUser "depends on (Phase 5 cutover)"
        memoryServer.api.contextRoute -> memoryServer.api.requireUser "depends on (Phase 5 cutover)"
        memoryServer.api.requireUser -> memoryServer.api.tokenCache "hot path: get(sha256(token))"
        memoryServer.api.requireUser -> keycloak "cold path: validate JWT against JWKS" "HTTP/JSON"
        memoryServer.api.requireUser -> memoryServer.api.tokenCache "cold path: put(sha256(token), UserContext, TTL)"
        memoryServer.api.tokenCache -> memoryServer.redisCache "GET/SETEX/SCAN under sovereign:token:*" "Redis protocol"

        // Legacy JWT path (kept for backwards compat until Phase 7)
        memoryServer.api.contextRoute -> memoryServer.api.requireScope "legacy: returns raw payload dict"
        memoryServer.api.requireScope -> keycloak "Fetch JWKS (shared cache)" "HTTP/JSON"

        // Relationships — chat completion flow (inject mode — memory_mode=inject)
        memoryServer.api.chatRoute -> memoryServer.api.contextBuilder "inject mode: build_system_context(UserContext, project, query)"
        memoryServer.api.contextRoute -> memoryServer.api.contextBuilder "build_system_context_with_stats()"
        memoryServer.api.chatRoute -> llamaServer "async httpx.AsyncClient.stream() (ADR-024)"
        memoryServer.api.chatRoute -> memoryServer.api.telemetry "@observe span + post-stream Langfuse update"

        // Relationships — chat completion flow (tools mode — memory_mode=tools, ADR-025)
        memoryServer.api.chatRoute -> memoryServer.api.memoryToolRegistry "tools mode: tools_visible_to(user) — scope-filtered tool list"
        memoryServer.api.chatRoute -> memoryServer.api.contextBuilder "tools mode: build_ambient_context(user, project, tools)"
        memoryServer.api.chatRoute -> memoryServer.api.memoryToolLoop "tools mode: run_memory_tool_loop(...)"
        memoryServer.api.memoryToolLoop -> llamaServer "non-streaming POST per iteration (bounded by max_iterations)"
        memoryServer.api.memoryToolLoop -> memoryServer.api.memoryToolRegistry "get_tool_by_name + invoke_tool dispatch"
        memoryServer.api.memoryToolRegistry -> memoryServer.api.toolResultCache "cache.get / cache.put (skip on exception)"
        memoryServer.api.memoryToolRegistry -> memoryServer.api.memoryHandlers "await tool.handler(user_context, args)"
        memoryServer.api.toolResultCache -> memoryServer.redisCache "GET/SETEX/SCAN under sovereign:tool-result:*" "Redis protocol"

        // Memory tool handlers wrap the existing 4-layer services
        memoryServer.api.memoryHandlers -> memoryServer.api.episodicSvc "recall_decisions → search(user, query)"
        memoryServer.api.memoryHandlers -> memoryServer.api.proceduralSvc "recall_skills → search(user, query)"
        memoryServer.api.memoryHandlers -> memoryServer.api.conversationalSvc "recall_recent_sessions → load_sessions(user, project, n)"
        memoryServer.api.memoryHandlers -> memoryServer.api.semanticSvc "recall_semantic → search(user, query, k)"

        // 4-layer memory retrieval (inject mode path)
        memoryServer.api.contextBuilder -> memoryServer.api.episodicSvc "search(user_context, query)"
        memoryServer.api.contextBuilder -> memoryServer.api.proceduralSvc "search(user_context, query)"
        memoryServer.api.contextBuilder -> memoryServer.api.conversationalSvc "as_context(user_context, project)"
        memoryServer.api.contextBuilder -> memoryServer.api.semanticSvc "search(user_context, query, k)"

        memoryServer.api.episodicSvc -> memoryServer.adrFiles "Reads ADR-*.md" "pathlib"
        memoryServer.api.proceduralSvc -> memoryServer.skillFiles "Reads SKILL-*.md" "pathlib"
        memoryServer.api.conversationalSvc -> memoryServer.postgresDb "SELECT/INSERT sessions" "SQLAlchemy ORM"
        memoryServer.api.semanticSvc -> memoryServer.chromaDb "query()" "HTTP + Bearer token"
        memoryServer.chromaDb -> embedServer "Embedding vectors" "HTTP/OpenAI-compat"

        // DI wiring
        memoryServer.api.diContainer -> memoryServer.api.contextBuilder "Injects"
        memoryServer.api.diContainer -> memoryServer.api.episodicSvc "Injects"
        memoryServer.api.diContainer -> memoryServer.api.proceduralSvc "Injects"
        memoryServer.api.diContainer -> memoryServer.api.conversationalSvc "Injects"
        memoryServer.api.diContainer -> memoryServer.api.semanticSvc "Injects"
        memoryServer.api.diContainer -> memoryServer.api.pgFactory "Injects"
        memoryServer.api.pgFactory -> memoryServer.postgresDb "create_engine() + sessionmaker" "SQLAlchemy"

        // Audit writes (post-stream / post-loop, synchronous — async persistence deferred to a separate ADR, see §12 brainstorm)
        memoryServer.api.chatRoute -> memoryServer.postgresDb "INSERT interactions (user_id = Keycloak sub)"
        memoryServer.api.chatRoute -> memoryServer.postgresDb "INSERT tool_calls (ADR-025: one row per memory tool invocation; cache hits skip)"

        // Deployment — Docker Compose
        deploymentEnvironment "Docker" {

            deploymentNode "Docker Compose" "sovereign-ai-stack" "Docker Compose v2" {

                deploymentNode "traefik" "TLS termination + reverse proxy" "Traefik v3" {
                    traefikInstance = infrastructureNode "Traefik" "HTTPS :443 → memory-server :8765" "Traefik v3"
                }

                deploymentNode "memory-server" "FastAPI application container" "Python 3.12 / uvicorn" {
                    apiInstance = containerInstance memoryServer.api
                }

                deploymentNode "postgres" "Audit + sessions — interactions, sessions, tool_calls (DESIGN §15)" "PostgreSQL 16 Alpine" {
                    pgInstance = containerInstance memoryServer.postgresDb
                }

                deploymentNode "redis" "Shared cache: sovereign:token:* (DESIGN §15.4) + sovereign:tool-result:* (ADR-025 §Decision.8). Dedicated — NOT shared with Langfuse Redis." "Redis 7 Alpine" {
                    redisInstance = containerInstance memoryServer.redisCache
                }

                deploymentNode "chromadb" "Vector store — token auth" "ChromaDB HTTP Server" {
                    chromaInstance = containerInstance memoryServer.chromaDb
                }

                deploymentNode "keycloak" "Identity provider — owns users/roles/JWT issuance" "Keycloak 24" {
                    keycloakInstance = infrastructureNode "Keycloak" "Realm: sovereign-ai, OIDC + private_key_jwt" "Keycloak 24"
                }
            }

            deploymentNode "Host Machine" "Unified memory workstation — bare metal GPU" "Linux / ROCm" {
                llamaInstance = infrastructureNode "llama-server" "Qwen3.5-35B-A3B on :11435" "llama.cpp / ROCm"
                embedInstance = infrastructureNode "embed-server" "nomic-embed-text on :11436" "llama.cpp / CPU"
            }

            deploymentNode "Langfuse Stack" "Sibling compose — shared network (ADR-021.2)" "Docker Compose" {
                langfuseInstance = infrastructureNode "Langfuse Web" "Observability traces + OTLP ingest" "Langfuse v3"
            }

            // Deployment relationships
            traefikInstance -> apiInstance "HTTPS → HTTP proxy"
            traefikInstance -> keycloakInstance "HTTPS → /realms/*, /admin/*"
            apiInstance -> keycloakInstance "JWKS fetch (cached)" "HTTP/JSON"
            apiInstance -> redisInstance "Token cache GET/SETEX" "Redis protocol"
            apiInstance -> llamaInstance "Proxies augmented requests (streaming)" "HTTP/SSE"
            apiInstance -> langfuseInstance "Exports traces + ingestion API updates" "HTTP/OTLP"
            chromaInstance -> embedInstance "Embedding vectors" "HTTP"
        }
    }

    views {

        systemContext memoryServer "SystemContext" "sovereign-memory-server — who uses it" {
            include *
            autolayout lr
        }

        container memoryServer "Containers" "sovereign-memory-server — deployable units (incl. Redis token cache)" {
            include *
            autolayout lr
        }

        component memoryServer.api "Components" "FastAPI application — 4-layer memory + Keycloak-delegated identity" {
            include *
            autolayout tb
        }

        deployment memoryServer "Docker" "DockerCompose" "Docker Compose topology — incl. sovereign-redis container (DESIGN §15.4)" {
            include *
            autolayout lr
        }

        styles {
            element "Person" {
                shape Person
                background #08427B
                color #ffffff
            }
            element "Software System" {
                background #1168BD
                color #ffffff
            }
            element "External" {
                background #999999
                color #ffffff
            }
            element "Container" {
                background #438DD5
                color #ffffff
            }
            element "Component" {
                background #85BBF0
                color #000000
            }
            element "Database" {
                shape Cylinder
            }
        }
    }
}
