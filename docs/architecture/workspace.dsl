workspace "audittrace-server" "Five-Layer Memory Augmentation Proxy for Local LLMs" {

    !impliedRelationships true

    model {

        // Actors
        agent = person "Coding Agent" "OpenCode / Roo Code / Continue (OpenAI-compatible)"
        architect = person "Luis Filipe" "Solutions Architect"
        humanUser = person "Human User" "OAuth2 Device Flow login (ADR-032)"
        mcpAgent = person "External MCP-speaking Agent" "Any MCP client (Claude Desktop, IDE plugin, CLI) speaking JSON-RPC 2.0 (ADR-063)"

        // External systems
        vault = softwareSystem "HashiCorp Vault" "In-cluster secret store (ADR-043)" {
            tags "External"
        }
        llamaServer = softwareSystem "Chat LLM Server" "Reasoning — Qwen 3.8-27B dense+MTP, :11435 (GPU)" {
            tags "External"
        }
        embedServer = softwareSystem "Embedding Server (nomic)" "Embeddings — nomic-embed-text v1.5, :11436 (live, ADR-047)" {
            tags "External"
        }
        summarizerServer = softwareSystem "Summariser LLM Server" "Background summaries — Mistral 7B, :11437 (ADR-030)" {
            tags "External"
        }
        langfuse = softwareSystem "Langfuse" "Observability — traces + spans; ephemeral corroborating witness, not the durable record (ADR-028 retention amendment)" {
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
        observability = softwareSystem "Observability Stack" "Prometheus + Grafana + Loki (ADR-028) — Tempo 7d / Loki 30d / Prometheus 30d, ephemeral corroborating witness (2026-08-22 retention amendment)" {
            tags "External"
        }
        opencodeProxy = softwareSystem "OpenCode TLS Proxy" "Caddy loopback → TLS (Bun fetch workaround)" {
            tags "External"
        }
        contentControl = softwareSystem "audittrace-content-control" "Scans uploads, publishes verdicts (ADR-048)" {
            tags "External"
        }
        rabbitmq = softwareSystem "RabbitMQ Broker" "Scan-control messaging (ADR-057) — incl. audittrace.scan.requests.dlq (quorum queue, x-delivery-limit=5), operator-drained via scripts/audittrace-scan-dlq" {
            tags "External"
        }
        downstreamMcpServers = softwareSystem "Downstream MCP Servers" "Operator-configured external MCP servers fronted by the broker (Settings.mcp_broker_servers, ADR-063 Phase 2 Track B)" {
            tags "External"
        }

        // The system
        memoryServer = softwareSystem "audittrace-server" "Augmentation proxy — five-layer memory + delegated identity" {

            !adrs adrs

            api = container "FastAPI Application" "OpenAI-compatible API + /context + /mcp" "Python / FastAPI" {

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

                // MCP entry-interface (ADR-063) — Phase 1 read tools + Phase 2
                // Track A write/curation tools + Phase 2 Track B broker, one
                // coherent surface over the SAME audit path the chat tool loop
                // uses. Additive: mounted in this SAME FastAPI process/pod as
                // /v1/chat/completions, never a separate container.
                mcpRoute = component "MCP Route" "POST /mcp — stateless JSON-RPC 2.0 entry-interface; require_user gates every method incl. tools/list (ADR-063)" "routes/mcp.py"
                mcpReadBridge = component "MCP Read Bridge" "Phase 1: authorize (read-scope only, no admin bypass) → execute → record → return over the existing read-tool registry" "services/mcp_bridge.py"
                mcpWriteBridge = component "MCP Write Bridge" "Phase 2 Track A: authorize (per-tool scope, no admin bypass, no cache) → execute → record → return over the write/curation tool registry" "services/mcp_write_bridge.py"
                mcpWriteRegistry = component "MCP Write Tool Registry" "MCP-only write/curation tool registry — structurally separate from MemoryToolRegistry so /v1/chat/completions is unaffected" "tools/mcp_write_registry.py"
                mcpWriteHandlers = component "MCP Write/Curation Handlers" "write_decision / write_skill — private-tier-only upsert, per-tool scope, no admin bypass, no cache" "tools/mcp_write_handlers.py"
                mcpBroker = component "MCP Broker" "Phase 2 Track B: downstream-tool registry + broker:<server>:<tool> dispatch fronting operator-configured external MCP servers; audits request+result either side of the network hop — honesty boundary, audits only what it actually brokers (ADR-037)" "services/mcp_broker.py"

                // Scan-control plane (ADR-057) — lifespan-owned background
                // tasks in this SAME FastAPI process, Hohpe Transactional
                // Outbox over RabbitMQ.
                scanRequestPublisher = component "Scan Request Publisher" "Outbox producer: MinIO quarantine PUT + memory_items INSERT (scan_status=pending_scan) → asyncio.Queue → AMQP publish" "services/scan_request_publisher.py"
                scanRequestJanitor = component "Scan Request Janitor" "Re-enqueues orphaned outbox rows past the grace window; scan_status IS NOT NULL is the load-bearing discriminator that excludes .md manifest folds from being misrouted as scan candidates (SCAN-URI-BUG fix, 2026-08-23)" "services/scan_request_janitor.py"
                scanVerdictConsumer = component "Scan Verdict Consumer" "Consumes scan.verdict.*  → scan_status + re-enqueues clean files for auto-index (ADR-048/ADR-057)" "services/scan_verdict_consumer.py"
                scanAuditConsumer = component "Scan Audit Consumer" "Consumes scan.audit.* → SECURITY audit rows" "services/scan_audit_consumer.py"
            }

            postgresDb = container "PostgreSQL 16" "Audit trail — interactions, sessions, tool_calls; no-expiry durable record, EU AI Act Art 12 (ADR-028 retention amendment)" "PostgreSQL" {
                tags "Database"
            }
            chromaDb = container "ChromaDB Server" "Vector store — token auth; no-expiry durable record (ADR-028 retention amendment)" "ChromaDB" {
                tags "Database"
            }
            redisCache = container "Redis 7" "Cache + streams — tokens, tool-results, persist" "Redis" {
                tags "Database"
            }
            minioStore = container "MinIO" "S3 object storage — shared + per-user (ADR-027); no-expiry durable record, explicit lifecycle = retain (ADR-027 retention amendment)" "MinIO" {
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

        // five-layer retrieval (inject path)
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

        // MCP entry-interface (ADR-063) — one audited surface: Phase 1 read
        // tools + Phase 2 Track A write/curation tools + Phase 2 Track B
        // broker. Authorize → execute/forward → record → return, through
        // the SAME tamper-evident path the chat tool loop uses.
        mcpAgent -> api "MCP entry-interface — audited tool calls (ADR-063)" "HTTP/JSON-RPC POST /mcp"
        mcpRoute -> requireUser "depends on (every JSON-RPC method incl. tools/list)"
        mcpRoute -> mcpReadBridge "own-tool dispatch — unnamespaced tool names (Phase 1)"
        mcpRoute -> mcpWriteBridge "get_write_tool_by_name — routing check (Phase 2 Track A)"
        mcpWriteBridge -> mcpWriteRegistry "looks up the registered write tool"
        mcpWriteBridge -> mcpWriteHandlers "call_write_tool(): tool.handler(user, args) on literal per-tool scope grant, no admin bypass"
        mcpRoute -> mcpBroker "broker:<server>:<tool> dispatch (Phase 2 Track B)"
        mcpReadBridge -> memoryToolRegistry "call_read_tool(): get_tool_by_name + invoke_tool (read-scope only, no admin bypass on the write boundary)"
        mcpWriteHandlers -> semanticSvc "upsert write_decision / write_skill — private tier only (ADR-062)"
        mcpWriteHandlers -> postgresDb "manifest row + tamper-evident memory-audit event (ADR-058)"
        mcpBroker -> downstreamMcpServers "Forwards tools/call under the caller's resolved identity — never a client-supplied identity; audits request+result either side (honesty boundary, ADR-037)" "HTTP/JSON-RPC"
        mcpRoute -> postgresDb "INSERT interactions + tool_calls — one trace per call; brokered calls record BOTH request+result (ADR-037/058)"

        // Ingestion content-control (ADR-048 / ADR-057) — Hohpe
        // Transactional Outbox: route → MinIO + manifest INSERT →
        // in-process queue → ScanRequestPublisher → AMQP.
        api -> minioStore "PUT quarantine/<user>/<uuid>/<file>" "S3 API"
        scanRequestPublisher -> rabbitmq "publish scan.request.* — durable, outbox pattern (ADR-057)" "AMQP (mTLS)"
        scanRequestJanitor -> postgresDb "SELECT orphaned rows — published_at_ms IS NULL AND scan_status IS NOT NULL, past the grace window (SCAN-URI-BUG discriminator)" "SQLAlchemy"
        scanRequestJanitor -> scanRequestPublisher "re-enqueues onto the shared outbox queue"
        rabbitmq -> contentControl "deliver scan requests" "AMQP (mTLS)"
        contentControl -> minioStore "GET quarantine, PUT episodic/papers/" "S3 API"
        contentControl -> rabbitmq "publish verdicts + audit rows" "AMQP (mTLS)"
        rabbitmq -> scanVerdictConsumer "deliver scan.verdict.*" "AMQP (mTLS)"
        rabbitmq -> scanAuditConsumer "deliver scan.audit.*" "AMQP (mTLS)"
        scanVerdictConsumer -> postgresDb "UPDATE memory_items scan_status; re-point key + reset indexed_at_ms on clean verdict (auto-index outbox)" "SQLAlchemy"
        architect -> rabbitmq "Inspect / replay / drain audittrace.scan.requests.dlq (scripts/audittrace-scan-dlq, ADR-057 operator recovery lever)" "AMQP (kubectl port-forward)"

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

            deploymentNode "Host Machine" "Bare-metal GPU — three model processes" "Linux / Vulkan" {
                llamaInstance = infrastructureNode "chat-llama-server" "Qwen 3.8-27B dense+MTP, :11435 (GPU)" "llama.cpp / Vulkan"
                embedInstance = infrastructureNode "embed-server (nomic)" "nomic-embed-text v1.5, :11436 (CPU) — live embedder (ADR-047)" "llama.cpp / CPU"
                summarizerInstance = infrastructureNode "summariser-llama-server" "Mistral 7B, :11437 (GPU, ADR-030)" "llama.cpp / Vulkan"
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

        component api "Components" "FastAPI application — five-layer memory + identity" {
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
