workspace "audittrace-server" "4-Layer Memory Augmentation Proxy for Local LLMs" {

    !identifiers hierarchical
    !impliedRelationships true

    model {

        // Actors
        agent = person "Coding Agent" "OpenCode, Roo Code, Continue — any OpenAI-compatible client"
        architect = person "Luis Filipe" "Solutions Architect — configures memory layers and ADRs"
        humanUser = person "Human User" "Browser-capable human authenticating via OAuth2 Device Flow (RFC 8628, ADR-032) so every agent request carries a real Keycloak sub on the audit trail instead of the flat dev-client identity"

        // External systems
        llamaServer = softwareSystem "Chat LLM Server" "Qwen3.5-35B-A3B Q4_K_M on :11435 — chat + tool-loop reasoning (GPU, ROCm)" {
            tags "External"
        }
        embedServer = softwareSystem "Embedding Server" "nomic-embed-text v1.5 Q8_0 on :11436 — 768-dim embeddings (CPU)" {
            tags "External"
        }
        summarizerServer = softwareSystem "Summariser LLM Server" "Mistral 7B Instruct v0.3 Q4_K_M on :11437 — background session summarisation, strict-JSON output (GPU, EU-origin) (ADR-030)" {
            tags "External"
        }
        langfuse = softwareSystem "Langfuse" "Observability — traces, spans, metrics (sibling stack)" {
            tags "External"
        }
        keycloak = softwareSystem "Keycloak" "Identity provider — owns users/roles/scopes; issues OIDC JWTs via both client_credentials (service accounts, ADR-022) AND OAuth2 Device Authorization Grant (human users, RFC 8628, ADR-032). Public client audittrace-opencode serves the Device Flow path; audittrace-dev serves the CI / smoke-test path." {
            tags "External"
        }
        observability = softwareSystem "Observability Stack" "Prometheus + Grafana + Loki + OTel Collector — metrics aggregation, log search, dashboards (ADR-028)" {
            tags "External"
        }

        // The system
        memoryServer = softwareSystem "audittrace-server" "Transparent augmentation proxy with 4-layer memory + Keycloak-delegated identity" {

            api = container "FastAPI Application" "OpenAI-compatible API + /context endpoint" "Python / FastAPI / uvicorn" {

                chatRoute = component "Chat Route" "/v1/chat/completions — async raw-dict pass-through; branches on memory_mode={inject|tools} (ADR-024, ADR-025)" "FastAPI Router"
                contextRoute = component "Context Route" "/context — retrieve assembled context" "FastAPI Router"
                healthRoute = component "Health Route" "/health, /metrics" "FastAPI Router"

                // Identity layer (DESIGN §15) — replaces the Phase 0/1 PAT model
                requireUser = component "require_user" "FastAPI dependency: validates Keycloak JWT via JWKS, accepts iss from the union of keycloak_issuer + keycloak_issuer_extras (ADR-032 §2 — lets Device-Flow tokens on the Istio-exposed issuer coexist with service-account tokens on the internal issuer), returns typed UserContext (DESIGN §15)" "auth.py"
                tokenCache = component "TokenCache" "sha256(token) → UserContext, TTL eviction, Redis-backed (DESIGN §15.4)" "identity.py"
                requireScope = component "require_scope (legacy)" "JWT scope check returning raw payload dict (ADR-022, ADR-023)" "auth.py"

                // Memory layers (inject mode + shared by tools mode handlers)
                contextBuilder = component "ContextBuilderService" "Aggregates all 4 memory layers (inject mode). In tools mode: build_ambient_context with intent-based selection rules guiding the LLM to pick ONE tool per question (ADR-025 Accepted)" "ABC + DefaultContextBuilder"
                episodicSvc = component "EpisodicService" "Layer 1 — ADR markdown loading from MinIO (ADR-027)" "ABC + S3EpisodicService (File fallback for tests)"
                proceduralSvc = component "ProceduralService" "Layer 2 — SKILL file loading from MinIO (ADR-027)" "ABC + S3ProceduralService (File fallback for tests)"
                conversationalSvc = component "ConversationalService" "Layer 3 — PostgreSQL sessions (ADR-020)" "ABC + PostgresConversationalService"
                semanticSvc = component "SemanticService" "Layer 4 — ChromaDB vector search" "ABC + ChromaSemanticService"

                // Memory-as-tools (ADR-025) — dynamic registry + tool-call loop
                memoryToolRegistry = component "MemoryToolRegistry" "Decorator-based registry (@register_memory_tool). tools_visible_to(user) scope filter. invoke_tool cache-aware dispatch. Optional TOML overlay (ADR-025)" "tools/__init__.py"
                memoryHandlers = component "Memory Tool Handlers" "recall_decisions, recall_skills, recall_recent_sessions, recall_semantic — thin adapters to the 4 services, canonical {matches, total, truncated} result schema" "tools/memory_handlers.py"
                toolResultCache = component "ToolResultCache" "sha256(session|tool|args) → result dict, TTL eviction, Redis-backed, namespace audittrace:tool-result:, disjoint from TokenCache (ADR-025 §Decision.8)" "tools/cache.py"
                memoryToolLoop = component "Memory Tool-Call Loop" "Proxy-internal non-streaming round-trip: dispatch → llama → tool_calls → execute selected memory tool → repeat. LLM selects ONE tool per question via ambient context selection rules. Bounded by AUDITTRACE_MEMORY_TOOL_LOOP_MAX_ITERATIONS. Produces PendingToolCall audit records (ADR-025 Accepted)" "routes/_memory_tool_loop.py"

                // Background summarisation (ADR-030)
                sessionSummarizer = component "SessionSummarizer" "Background asyncio task — every AUDITTRACE_SUMMARIZER_INTERVAL_MINUTES picks idle sessions (last interaction > AUDITTRACE_SUMMARIZER_IDLE_MINUTES) via FOR UPDATE SKIP LOCKED, calls the summariser LLM with strict-JSON response_format, writes SessionRecord rows. Sets app.current_user_id GUC per-session for RLS attribution (ADR-030)" "services/session_summarizer.py"

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
            redisCache = container "Redis 7" "Shared cache — audittrace:token:* for TokenCache (DESIGN §15.4) and audittrace:tool-result:* for ToolResultCache (ADR-025 §Decision.8). Disjoint key prefixes." "Redis" {
                tags "Database"
            }
            minioStore = container "MinIO" "S3-compatible object storage — memory-shared (ADRs, skills) + memory-private (per-user knowledge). SSE-S3 encryption at rest (ADR-027)" "MinIO" {
                tags "Database"
            }
        }

        // Relationships — system level
        humanUser -> keycloak "Interactive Device Flow login via browser (client_id=audittrace-opencode) — ADR-032" "HTTPS/OIDC"
        humanUser -> agent "Launches via scripts/opencode-wrapper.sh (Bearer merged into ~/.config/opencode/config.json)" "CLI"
        agent -> keycloak "Token refresh + service-account client_credentials (audittrace-dev)" "HTTPS/OIDC"
        agent -> memoryServer.api "POST /v1/chat/completions (Bearer JWT)" "HTTPS/JSON"
        architect -> memoryServer.minioStore "Uploads ADRs + skills via seed-memory.py (ADR-027)"
        memoryServer.api -> llamaServer "Proxies augmented request (async, streaming)" "HTTP/SSE"
        memoryServer.api -> keycloak "Fetch JWKS public keys (cached 5 min)" "HTTP/JSON"
        memoryServer.api -> langfuse "Exports traces via Langfuse SDK (ADR-024)" "HTTP/OTLP"
        memoryServer.api -> observability "Exports metrics + logs via OTLP to OTel Collector (ADR-028)" "HTTP/OTLP"

        // Relationships — identity layer (DESIGN §15)
        memoryServer.api.chatRoute -> memoryServer.api.requireUser "depends on (Phase 5 cutover)"
        memoryServer.api.contextRoute -> memoryServer.api.requireUser "depends on (Phase 5 cutover)"
        memoryServer.api.requireUser -> memoryServer.api.tokenCache "hot path: get(sha256(token))"
        memoryServer.api.requireUser -> keycloak "cold path: validate JWT against JWKS" "HTTP/JSON"
        memoryServer.api.requireUser -> memoryServer.api.tokenCache "cold path: put(sha256(token), UserContext, TTL)"
        memoryServer.api.tokenCache -> memoryServer.redisCache "GET/SETEX/SCAN under audittrace:token:*" "Redis protocol"

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
        memoryServer.api.toolResultCache -> memoryServer.redisCache "GET/SETEX/SCAN under audittrace:tool-result:*" "Redis protocol"

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

        memoryServer.api.episodicSvc -> memoryServer.minioStore "Lists + downloads ADR-*.md from memory-shared/episodic/" "S3 API"
        memoryServer.api.proceduralSvc -> memoryServer.minioStore "Lists + downloads SKILL-*.md from memory-shared/procedural/" "S3 API"
        memoryServer.api.conversationalSvc -> memoryServer.postgresDb "SELECT/INSERT sessions" "SQLAlchemy ORM"
        memoryServer.api.semanticSvc -> memoryServer.chromaDb "query()" "HTTP + Bearer token"
        memoryServer.chromaDb -> embedServer "Embedding vectors" "HTTP/OpenAI-compat"

        // Session summariser (ADR-030)
        memoryServer.api.sessionSummarizer -> memoryServer.postgresDb "Eligibility query + INSERT sessions (SET LOCAL app.current_user_id per row)" "SQLAlchemy ORM"
        memoryServer.api.sessionSummarizer -> memoryServer.api.conversationalSvc "save_session(user_context, project, summary, key_points, session_id=…)"
        memoryServer.api.sessionSummarizer -> summarizerServer "POST /v1/chat/completions (non-streaming, response_format=json_object)" "HTTP/JSON"

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

        // Deployment — Kubernetes + Istio
        deploymentEnvironment "Kubernetes" {

            deploymentNode "k3s Cluster" "pcluislinux" "k3s v1.34 + Istio 1.29" {

                deploymentNode "Namespace: istio-system" "Istio control plane" "Kubernetes" {
                    istiodInstance = infrastructureNode "istiod" "Service mesh control plane — xDS, cert rotation, policy" "Istio 1.29"
                }

                deploymentNode "Namespace: audittrace" "Istio sidecar injection enabled" "Kubernetes" {

                    deploymentNode "Istio IngressGateway" "TLS termination + routing" "Envoy" {
                        ingressInstance = infrastructureNode "Istio IngressGateway" "HTTPS :443 → audittrace-server :8765, /realms/* → keycloak" "Envoy"
                    }

                    deploymentNode "audittrace-server Deployment" "FastAPI application pod" "Python 3.12 / uvicorn" {
                        apiInstance = containerInstance memoryServer.api
                    }

                    deploymentNode "postgresql StatefulSet" "Audit + sessions — interactions, sessions, tool_calls (DESIGN §15)" "PostgreSQL 16 Alpine" {
                        pgInstance = containerInstance memoryServer.postgresDb
                    }

                    deploymentNode "redis StatefulSet" "Shared cache: audittrace:token:* (DESIGN §15.4) + audittrace:tool-result:* (ADR-025 §Decision.8). Dedicated — NOT shared with Langfuse Redis." "Redis 7 Alpine" {
                        redisInstance = containerInstance memoryServer.redisCache
                    }

                    deploymentNode "chromadb StatefulSet" "Vector store — token auth" "ChromaDB HTTP Server" {
                        chromaInstance = containerInstance memoryServer.chromaDb
                    }

                    deploymentNode "minio StatefulSet" "S3 object storage — SSE-S3 encryption at rest, per-user isolation via path prefix (ADR-027)" "MinIO" {
                        minioInstance = containerInstance memoryServer.minioStore
                    }

                    deploymentNode "keycloak Deployment" "Identity provider — owns users/roles/JWT issuance" "Keycloak 24" {
                        keycloakInstance = infrastructureNode "Keycloak" "Realm: audittrace, OIDC + private_key_jwt" "Keycloak 24"
                    }

                    deploymentNode "otel-collector DaemonSet" "OTLP receiver → Prometheus + Loki fan-out" "otel-contrib" {
                        otelCollectorInstance = infrastructureNode "OTel Collector" "OTLP receiver → Prometheus + Loki fan-out (mesh-local)" "otel-contrib"
                    }
                }
            }

            deploymentNode "Host Machine" "Unified memory workstation — bare metal GPU (three model processes, separate ports)" "Linux / ROCm" {
                llamaInstance = infrastructureNode "chat-llama-server" "Qwen3.5-35B-A3B Q4_K_M on :11435 (GPU)" "llama.cpp / ROCm"
                embedInstance = infrastructureNode "embed-server" "nomic-embed-text v1.5 Q8_0 on :11436 (CPU)" "llama.cpp / CPU"
                summarizerInstance = infrastructureNode "summariser-llama-server" "Mistral 7B Instruct v0.3 Q4_K_M on :11437 (GPU, ADR-030)" "llama.cpp / ROCm"
            }

            deploymentNode "Langfuse Stack" "Sibling compose — shared network (ADR-021.2)" "Docker Compose" {
                langfuseInstance = infrastructureNode "Langfuse Web" "Observability traces + OTLP ingest" "Langfuse v3"
            }

            // Deployment relationships
            ingressInstance -> apiInstance "HTTPS → HTTP proxy (mTLS via Istio sidecar)"
            ingressInstance -> keycloakInstance "HTTPS → /realms/*, /admin/*"
            apiInstance -> keycloakInstance "JWKS fetch (cached)" "HTTP/JSON"
            apiInstance -> redisInstance "Token cache GET/SETEX" "Redis protocol"
            apiInstance -> minioInstance "S3 API — memory-shared + memory-private buckets (ADR-027)" "HTTP/S3"
            apiInstance -> llamaInstance "Proxies augmented requests (streaming)" "HTTP/SSE"
            apiInstance -> summarizerInstance "Background summariser loop — non-streaming JSON (ADR-030)" "HTTP/JSON"
            apiInstance -> langfuseInstance "Exports traces via SDK" "HTTP/OTLP"
            apiInstance -> otelCollectorInstance "OTLP metrics + logs" "HTTP/OTLP"
            chromaInstance -> embedInstance "Embedding vectors" "HTTP"
        }
    }

    views {

        systemContext memoryServer "SystemContext" "audittrace-server — who uses it" {
            include *
            autolayout lr
        }

        container memoryServer "Containers" "audittrace-server — deployable units (incl. Redis token cache)" {
            include *
            autolayout lr
        }

        component memoryServer.api "Components" "FastAPI application — 4-layer memory + Keycloak-delegated identity" {
            include *
            autolayout tb
        }

        deployment memoryServer "Kubernetes" "K8sIstio" "k3s + Istio topology — Namespace audittrace with Envoy sidecars" {
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
