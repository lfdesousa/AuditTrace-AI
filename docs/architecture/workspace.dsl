workspace "audittrace-server" "4-Layer Memory Augmentation Proxy for Local LLMs" {

    !impliedRelationships true

    model {

        // Actors
        agent = person "Coding Agent" "OpenCode / Roo Code / Continue (OpenAI-compatible)"
        architect = person "Luis Filipe" "Solutions Architect"
        humanUser = person "Human User" "OAuth2 Device Flow login (ADR-032)"

        // External systems
        vault = softwareSystem "HashiCorp Vault" "In-cluster secret store (ADR-043)" {
            tags "External"
        }
        llamaServer = softwareSystem "Chat LLM Server" "Reasoning — Qwen 3.6-35B-A3B MoE, :11435 (GPU)" {
            tags "External"
        }
        embedServer = softwareSystem "Embedding Server (nomic)" "Embeddings — nomic-embed-text v1.5, :11436 (live, ADR-047)" {
            tags "External"
        }
        summarizerServer = softwareSystem "Summariser LLM Server" "Background summaries — Mistral 7B, :11437 (ADR-030)" {
            tags "External"
        }
        langfuse = softwareSystem "Langfuse" "Observability — traces + spans" {
            tags "External"
        }
        keycloak = softwareSystem "Keycloak" "Identity provider — OIDC JWTs (ADR-022/032)" {
            tags "External"
        }
        organisationalIdP = softwareSystem "Organisational IdP" "Customer OIDC issuer, brokered (ADR-044)" {
            tags "External"
        }
        euLotl = softwareSystem "EU List of Trusted Lists" "EU trust-list registry for PAdES (ADR-052)" {
            tags "External"
        }
        observability = softwareSystem "Observability Stack" "Prometheus + Grafana + Loki (ADR-028)" {
            tags "External"
        }
        opencodeProxy = softwareSystem "OpenCode TLS Proxy" "Caddy loopback → TLS (Bun fetch workaround)" {
            tags "External"
        }
        contentControl = softwareSystem "audittrace-content-control" "Scans uploads, publishes verdicts (ADR-048)" {
            tags "External"
        }
        rabbitmq = softwareSystem "RabbitMQ Broker" "Scan-control messaging (ADR-057)" {
            tags "External"
        }

        // The system
        memoryServer = softwareSystem "audittrace-server" "Augmentation proxy — 4-layer memory + delegated identity" {

            api = container "FastAPI Application" "OpenAI-compatible API + /context" "Python / FastAPI" {

                chatRoute = component "Chat Route" "/v1/chat/completions (inject|tools)" "FastAPI Router"
                contextRoute = component "Context Route" "/context" "FastAPI Router"
                healthRoute = component "Health Route" "/health, /metrics" "FastAPI Router"

                requireUser = component "require_user" "Validates Keycloak JWT → UserContext" "auth.py"
                tokenCache = component "TokenCache" "sha256(token) → UserContext, Redis-backed" "identity.py"
                requireScope = component "require_scope (legacy)" "JWT scope check (ADR-022)" "auth.py"

                contextBuilder = component "ContextBuilderService" "Aggregates the 4 memory layers (ADR-025)" "DefaultContextBuilder"
                episodicSvc = component "EpisodicService" "Layer 1 — ADRs from MinIO (ADR-027)" "S3EpisodicService"
                proceduralSvc = component "ProceduralService" "Layer 2 — SKILLs from MinIO (ADR-027)" "S3ProceduralService"
                conversationalSvc = component "ConversationalService" "Layer 3 — PostgreSQL sessions (ADR-020)" "PostgresConversationalService"
                semanticSvc = component "SemanticService" "Layer 4 — ChromaDB vector search" "ChromaSemanticService"
                embedder = component "Nomic Embed Client" "httpx → nomic /v1/embeddings (ADR-047); peer.service=nomic-embed-server" "httpx / OTel-instrumented"

                memoryToolRegistry = component "MemoryToolRegistry" "Scope-filtered tool registry (ADR-025)" "tools/__init__.py"
                memoryHandlers = component "Memory Tool Handlers" "recall_decisions / skills / sessions / semantic" "tools/memory_handlers.py"
                toolResultCache = component "ToolResultCache" "Tool-result cache, Redis-backed (ADR-025)" "tools/cache.py"
                memoryToolLoop = component "Memory Tool-Call Loop" "dispatch → llama → tool → repeat (ADR-025)" "routes/_memory_tool_loop.py"

                sessionSummarizer = component "SessionSummarizer" "Scheduled sweep: every 5 min, idle > 15 min → summariser LLM (ADR-030)" "services/session_summarizer.py"

                asyncPersistConsumer = component "AsyncPersistConsumer" "Redis-stream consumer → persist (ADR-046)" "services/async_persist.py"
                asyncPersistProducer = component "AsyncPersistProducer" "Opt-in async persist via Redis stream (ADR-046)" "services/async_persist.py"

                diContainer = component "DependencyContainer" "DI — factories + services" "Python"
                pgFactory = component "PostgresFactory" "Connection pooling (ADR-020)" "URLPostgresFactory"
                telemetry = component "Telemetry" "OTel spans + Langfuse SDK (ADR-024)" "telemetry.py"

                adminRoute = component "Admin Route" "/admin/trust-store/refresh (ADR-052)" "routes/admin.py"
                trustStoreProvider = component "TrustStoreProvider" "Where the PEM lives — S3 default (ADR-052)" "S3TrustStoreProvider"
                trustStoreBuilder = component "TrustStoreBuilder" "Where the PEM comes from — EU LOTL (ADR-052)" "EuLotlTrustStoreBuilder"
            }

            postgresDb = container "PostgreSQL 16" "Audit trail — interactions, sessions, tool_calls" "PostgreSQL" {
                tags "Database"
            }
            chromaDb = container "ChromaDB Server" "Vector store — token auth" "ChromaDB" {
                tags "Database"
            }
            redisCache = container "Redis 7" "Cache + streams — tokens, tool-results, persist" "Redis" {
                tags "Database"
            }
            minioStore = container "MinIO" "S3 object storage — shared + per-user (ADR-027)" "MinIO" {
                tags "Database"
            }
        }

        // Relationships — system level
        humanUser -> keycloak "Device Flow login (ADR-032)" "HTTPS/OIDC"
        humanUser -> organisationalIdP "Login via customer IdP (ADR-044)" "HTTPS/OIDC"
        keycloak -> organisationalIdP "OIDC broker handshake (ADR-044)" "HTTPS/OIDC"
        humanUser -> agent "Launches via opencode-wrapper" "CLI"
        agent -> keycloak "Token refresh / client_credentials" "HTTPS/OIDC"
        agent -> opencodeProxy "POST /v1/chat/completions (Bearer)" "HTTP/JSON"
        opencodeProxy -> api "Forwards over verified TLS" "HTTPS/JSON"
        agent -> api "Direct path (Continue / Roo / curl)" "HTTPS/JSON"
        architect -> minioStore "Uploads ADRs + skills (ADR-027)"
        api -> llamaServer "Proxies augmented request — peer.service=qwen-chat-llm" "HTTP/SSE"
        api -> keycloak "Fetch JWKS (cached)" "HTTP/JSON"
        api -> langfuse "Exports traces (ADR-024)" "HTTP/OTLP"
        api -> observability "Exports metrics + logs (ADR-028)" "HTTP/OTLP"
        api -> vault "Reads creds via Vault Agent (ADR-043)" "file-mount"
        keycloak -> vault "Reads admin + DB creds (ADR-043)" "file-mount"

        // Identity layer
        chatRoute -> requireUser "depends on"
        contextRoute -> requireUser "depends on"
        requireUser -> tokenCache "get(sha256(token))"
        requireUser -> keycloak "cold path: validate JWT vs JWKS" "HTTP/JSON"
        tokenCache -> redisCache "GET/SETEX audittrace:token:*" "Redis"
        contextRoute -> requireScope "legacy path"
        requireScope -> keycloak "Fetch JWKS" "HTTP/JSON"

        // Chat flow — inject mode
        chatRoute -> contextBuilder "inject: build_system_context()"
        contextRoute -> contextBuilder "build_system_context_with_stats()"
        chatRoute -> llamaServer "async stream() — peer.service=qwen-chat-llm" "HTTP/SSE"
        chatRoute -> telemetry "@observe span + Langfuse update"

        // Chat flow — tools mode (ADR-025)
        chatRoute -> memoryToolRegistry "tools_visible_to(user)"
        chatRoute -> contextBuilder "build_ambient_context()"
        chatRoute -> memoryToolLoop "run_memory_tool_loop()"
        memoryToolLoop -> llamaServer "non-streaming POST/iteration — peer.service=qwen-chat-llm" "HTTP/JSON"
        memoryToolLoop -> memoryToolRegistry "get_tool + invoke_tool"
        memoryToolRegistry -> toolResultCache "cache get/put"
        memoryToolRegistry -> memoryHandlers "tool.handler(user, args)"
        toolResultCache -> redisCache "GET/SETEX audittrace:tool-result:*" "Redis"

        // Memory tool handlers → services
        memoryHandlers -> episodicSvc "recall_decisions"
        memoryHandlers -> proceduralSvc "recall_skills"
        memoryHandlers -> conversationalSvc "recall_recent_sessions"
        memoryHandlers -> semanticSvc "recall_semantic"

        // 4-layer retrieval (inject path)
        contextBuilder -> episodicSvc "search()"
        contextBuilder -> proceduralSvc "search()"
        contextBuilder -> conversationalSvc "as_context()"
        contextBuilder -> semanticSvc "search()"

        episodicSvc -> minioStore "GET ADR-*.md (memory-shared/episodic/)" "S3 API"
        proceduralSvc -> minioStore "GET SKILL-*.md (memory-shared/procedural/)" "S3 API"
        conversationalSvc -> postgresDb "SELECT/INSERT sessions" "SQLAlchemy"
        semanticSvc -> chromaDb "query()/upsert() — pre-computed vectors" "HTTP + token"
        semanticSvc -> embedder "vectorise query/index (embed_via_nomic)" "in-proc call"
        embedder -> embedServer "POST /v1/embeddings (768-dim) — peer.service=nomic-embed-server (ADR-047)" "HTTP/JSON"

        // Session summariser (ADR-030)
        sessionSummarizer -> postgresDb "Eligibility query + INSERT sessions" "SQLAlchemy"
        sessionSummarizer -> conversationalSvc "save_session(summary, key_points)"
        sessionSummarizer -> summarizerServer "Scheduled sweep (5 min / idle >15 min) — peer.service=mistral-summariser-llm" "HTTP/JSON"

        // PAdES trust store (ADR-052)
        adminRoute -> requireUser "scope audittrace:admin"
        adminRoute -> trustStoreBuilder "build()"
        adminRoute -> trustStoreProvider "store() + invalidate"
        trustStoreBuilder -> euLotl "fetch + verify XAdES (transient)" "HTTPS/XML"
        trustStoreProvider -> minioStore "GET/PUT trust-store bundle" "S3 API"
        diContainer -> trustStoreProvider "injects"
        diContainer -> trustStoreBuilder "injects"

        // Async chat persistence (ADR-046)
        chatRoute -> asyncPersistProducer "on X-Persist-Mode: async"
        asyncPersistProducer -> redisCache "XADD persist:stream" "Redis Streams"
        redisCache -> asyncPersistConsumer "XREADGROUP / XCLAIM" "Redis Streams"
        asyncPersistConsumer -> postgresDb "_persist_interaction → XACK" "SQLAlchemy"
        asyncPersistConsumer -> redisCache "XADD persist:dlq on poison" "Redis Streams"

        // Ingestion content-control (ADR-048 / ADR-057)
        api -> minioStore "PUT quarantine/<user>/<uuid>/<file>" "S3 API"
        api -> rabbitmq "publish scan.request.* (ADR-057)" "AMQP (mTLS)"
        rabbitmq -> contentControl "deliver scan requests" "AMQP (mTLS)"
        contentControl -> minioStore "GET quarantine, PUT episodic/papers/" "S3 API"
        contentControl -> rabbitmq "publish verdicts + audit rows" "AMQP (mTLS)"
        rabbitmq -> api "deliver verdicts → scan_status + security rows" "AMQP (mTLS)"

        // DI wiring
        diContainer -> contextBuilder "injects"
        diContainer -> episodicSvc "injects"
        diContainer -> proceduralSvc "injects"
        diContainer -> conversationalSvc "injects"
        diContainer -> semanticSvc "injects"
        diContainer -> pgFactory "injects"
        pgFactory -> postgresDb "create_engine + sessionmaker" "SQLAlchemy"

        // Audit writes
        chatRoute -> postgresDb "INSERT interactions (user_id = Keycloak sub)"
        chatRoute -> postgresDb "INSERT tool_calls (ADR-025)"

        // Deployment — Kubernetes + Istio
        deploymentEnvironment "Kubernetes" {

            deploymentNode "k3s Cluster" "pcluislinux" "k3s v1.34 + Istio 1.29" {

                deploymentNode "Namespace: istio-system" "Istio control plane" "Kubernetes" {
                    istiodInstance = infrastructureNode "istiod" "Service mesh control plane" "Istio 1.29"
                }

                deploymentNode "Namespace: audittrace" "Istio sidecar injection" "Kubernetes" {

                    deploymentNode "Istio IngressGateway" "TLS termination + routing" "Envoy" {
                        ingressInstance = infrastructureNode "Istio IngressGateway" "HTTPS :443 → :8765" "Envoy"
                    }

                    deploymentNode "audittrace-server Deployment" "FastAPI pod" "Python 3.12 / uvicorn" {
                        apiInstance = containerInstance api
                    }

                    deploymentNode "postgresql StatefulSet" "Audit + sessions" "PostgreSQL 16" {
                        pgInstance = containerInstance postgresDb
                    }

                    deploymentNode "redis StatefulSet" "Cache + streams (dedicated)" "Redis 7" {
                        redisInstance = containerInstance redisCache
                    }

                    deploymentNode "chromadb StatefulSet" "Vector store — token auth" "ChromaDB" {
                        chromaInstance = containerInstance chromaDb
                    }

                    deploymentNode "minio StatefulSet" "S3 object storage (ADR-027)" "MinIO" {
                        minioInstance = containerInstance minioStore
                    }

                    deploymentNode "keycloak Deployment" "Identity provider" "Keycloak 24" {
                        keycloakInstance = infrastructureNode "Keycloak" "Realm: audittrace, OIDC" "Keycloak 24"
                    }

                    deploymentNode "otel-collector DaemonSet" "OTLP → Prometheus + Loki" "otel-contrib" {
                        otelCollectorInstance = infrastructureNode "OTel Collector" "OTLP receiver + fan-out" "otel-contrib"
                    }

                    deploymentNode "vault StatefulSet" "Single-node, file backend (ADR-043)" "Vault 1.x" {
                        vaultServerInstance = infrastructureNode "vault-server" "KV v2 + K8s auth" "Vault server"
                        vaultInjectorInstance = infrastructureNode "vault-agent-injector" "Mutating webhook — Agent sidecars" "vault-k8s"
                    }
                }
            }

            deploymentNode "Host Machine" "Bare-metal GPU — three model processes" "Linux / ROCm" {
                llamaInstance = infrastructureNode "chat-llama-server" "Qwen 3.6-35B-A3B MoE, :11435 (GPU)" "llama.cpp / ROCm"
                embedInstance = infrastructureNode "embed-server (nomic)" "nomic-embed-text v1.5, :11436 (CPU) — live embedder (ADR-047)" "llama.cpp / CPU"
                summarizerInstance = infrastructureNode "summariser-llama-server" "Mistral 7B, :11437 (GPU, ADR-030)" "llama.cpp / ROCm"
                opencodeProxyInstance = infrastructureNode "audittrace-opencode-proxy" "Caddy loopback → TLS (systemd)" "Caddy 2.6"
                vaultUnsealInstance = infrastructureNode "audittrace-vault-auto-unseal" "Boot-time Vault unseal (systemd)" "systemd + bash"
            }

            deploymentNode "Langfuse Stack" "Sibling compose" "Docker Compose" {
                langfuseInstance = infrastructureNode "Langfuse Web" "Traces + OTLP ingest" "Langfuse v3"
            }

            // Deployment relationships
            opencodeProxyInstance -> ingressInstance "Forwards (Host=audittrace.local, cert-pinned)" "HTTPS/JSON"
            vaultUnsealInstance -> vaultServerInstance "Posts unseal keys at boot" "Vault API"
            ingressInstance -> apiInstance "HTTPS → HTTP (mTLS via sidecar)"
            ingressInstance -> keycloakInstance "HTTPS → /realms/*"
            apiInstance -> vaultServerInstance "Reads creds via Vault Agent (ADR-043)" "file-mount"
            keycloakInstance -> vaultServerInstance "Reads admin pw + DB creds (ADR-043)" "file-mount"
            apiInstance -> keycloakInstance "JWKS fetch (cached)" "HTTP/JSON"
            apiInstance -> llamaInstance "Reasoning — peer.service=qwen-chat-llm" "HTTP/SSE"
            apiInstance -> embedInstance "Embeddings — peer.service=nomic-embed-server (ADR-047)" "HTTP/JSON"
            apiInstance -> summarizerInstance "Scheduled summaries (5 min) — peer.service=mistral-summariser-llm" "HTTP/JSON"
            apiInstance -> langfuseInstance "Exports traces" "HTTP/OTLP"
            apiInstance -> otelCollectorInstance "OTLP metrics + logs" "HTTP/OTLP"
        }
    }

    views {

        systemContext memoryServer "SystemContext" "audittrace-server — who uses it" {
            include *
            autolayout lr
        }

        container memoryServer "Containers" "audittrace-server — deployable units" {
            include *
            autolayout lr
        }

        component api "Components" "FastAPI application — 4-layer memory + identity" {
            include *
            autolayout tb
        }

        deployment memoryServer "Kubernetes" "K8sIstio" "k3s + Istio topology" {
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
