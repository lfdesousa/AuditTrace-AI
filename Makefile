.PHONY: help venv install install-hooks lint security-lint test test-cov test-coverage clean \
       docker-build docker-run k8s-build k8s-install k8s-upgrade k8s-status k8s-template \
       deploy-preflight verify-deploy sync-requirements check-requirements-sync check-pr-body \
       token-guard sync-dashboards check-dashboard-drift

# SPEC #441 — the obs-stack (ADR-028, external docker-compose project) is a
# sibling checkout, not part of this repo. Its Grafana file-provider dir is a
# synced mirror of the chart's canonical dashboard set; the path is
# env-parameterized so every target here stays safe where no obs-stack exists.
OBS_STACK_DIR ?= $(HOME)/work/observability-stack

help: ## Show this help message
	@echo 'Usage: make <target>'
	@echo ''
	@echo 'Available targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

venv: ## Create virtual environment
	@echo "🐍 Creating virtual environment..."
	@python3 -m venv .venv
	@echo "✅ Virtual environment created"
	@echo ""
	@echo "Activate with: source .venv/bin/activate"

install: venv ## Install all dependencies (including dev)
	@echo "📦 Installing dependencies..."
	@.venv/bin/pip install --upgrade pip
	@.venv/bin/pip install -e ".[dev]"
	@echo "✅ Dependencies installed"
	@.venv/bin/pre-commit install
	@$(MAKE) install-hooks
	@echo ""
	@echo "Run tests: make test"

install-hooks: ## Install the tracked pre-push hook (.githooks/pre-push -> git hooks dir). Idempotent, composes with gitleaks; does NOT touch core.hooksPath (pre-commit refuses `install` when it's set — see scripts/install-git-hooks.sh). #397.
	@bash scripts/install-git-hooks.sh

lint: security-lint ## Run linting and formatting (includes the offline security-lint gate)
	@echo "🔍 Running linter..."
	@.venv/bin/ruff check src/ tests/
	@echo "✅ Linting passed"
	@echo "📝 Running formatter check..."
	@.venv/bin/ruff format --check src/ tests/
	@echo "✅ Formatting passed"

security-lint: ## Run the OFFLINE, deterministic semgrep security-lint gate (#382, ADR-059 WS1). GATEKEEPER tooling — install first with `pip install -e '.[gate]'` (kept OUT of the image on purpose). Vendored rules only — no network, no `--config auto`. Composable: slots into the unified local CI-Agent gate.
	@SEMGREP="$$( [ -x .venv/bin/semgrep ] && echo .venv/bin/semgrep || command -v semgrep || true )"; \
	if [ -z "$$SEMGREP" ]; then \
	  echo "❌ semgrep not found. It is the CI-Agent gate (gatekeeper) tooling —"; \
	  echo "   deliberately NOT a runtime/image/test dependency. Install it ISOLATED"; \
	  echo "   (pipx gives it its own venv so it can't downgrade the app's otel):"; \
	  echo "       pipx install 'semgrep==1.171.0'   # pin == pyproject [gate]"; \
	  echo "   Do NOT 'pip install -e .[gate]' into the app/.venv — semgrep pins"; \
	  echo "   opentelemetry-instrumentation-requests~=0.58b0 and drags the app's"; \
	  echo "   otel line down from 0.65b0, breaking FastAPI route detection."; \
	  exit 1; \
	fi; \
	echo "🛡️  Running semgrep security-lint (offline, vendored rules) via $$SEMGREP ..."; \
	"$$SEMGREP" \
	  --error \
	  --disable-version-check \
	  --metrics=off \
	  --config ci/semgrep/rules.yml \
	  src/ tests/ scripts/; \
	echo "✅ Security-lint passed"

token-guard: ## TOKEN-GUARD agent-def drift guard (ADR-059 layer 3, SPEC token-guard-kill-show-20260811). Fails if any agent def teaches the raw `audittrace-login --show` form or a direct tokens.json read. Scans the operator's real (gitignored) .claude/agents/*.md + .claude/settings.local.json, and the private sdlc/agents/** fleet defs if present. Best-effort — never fails when no targets are found (e.g. CI has no private checkout). Wired into pre-commit (id token-guard, always_run) so it's mechanically enforced, not prose (feedback_policies_must_be_mechanically_inviolable). Python resolution is PORTABLE (laptop .venv/ vs CI hostedtoolcache with no repo-local .venv/) — same fallback idiom as security-lint's semgrep lookup.
	@TG_PYTHON="$$( [ -x .venv/bin/python ] && echo .venv/bin/python || command -v python3 )"; \
	"$$TG_PYTHON" scripts/check-agent-def-token-guard.py

format: ## Run code formatting
	@echo "📝 Running code formatter..."
	@.venv/bin/ruff check --fix src/ tests/
	@.venv/bin/ruff format src/ tests/
	@echo "✅ Code formatted"

typecheck: ## Run type checking
	@echo "🔎 Running type checker..."
	@.venv/bin/mypy src/
	@echo "✅ Type checking passed"

test: ## Run all tests with per-file coverage gate
	@echo "🧪 Running tests..."
	@.venv/bin/pytest tests/ -v --cov=src --cov=scripts/deploy --cov=scripts/hooks --cov=scripts/release --cov=scripts/migrate --cov=scripts/curator --cov=scripts/network --cov-report=term-missing --cov-report=xml --cov-fail-under=90 --junit-xml=junit.xml
	@echo "🔒 Enforcing per-file coverage gate (each component >= 90%)..."
	@.venv/bin/python scripts/check-per-file-coverage.py
	@echo "🚫 Enforcing zero-skip policy..."
	@.venv/bin/python scripts/check-no-skipped-tests.py
	@echo "✅ Tests passed"

test-rls-local: ## Run RLS integration tests against an ephemeral Docker Postgres (NEVER production)
	# Runs tests/test_rls_isolation.py against a throwaway postgres:16
	# container on localhost:15432. CRITICALLY: does NOT port-forward the
	# k3s production Postgres — the test file includes a positive test
	# (test_alice_cannot_insert_as_bob) that deliberately provokes an RLS
	# violation to prove WITH CHECK works. That violation gets logged as
	# an ERROR by Postgres unconditionally, which would pollute production
	# logs. An ephemeral container keeps the noise scoped.
	@echo "🐳 Spinning up ephemeral test Postgres on :15432 ..."
	@docker rm -f audittrace-test-pg >/dev/null 2>&1 || true
	@docker run -d --rm --name audittrace-test-pg \
	  -e POSTGRES_USER=postgres \
	  -e POSTGRES_PASSWORD=test \
	  -e POSTGRES_DB=audittrace \
	  -p 15432:5432 \
	  postgres:16 >/dev/null
	@echo "⏳ Waiting for Postgres to accept connections ..."
	@for i in 1 2 3 4 5 6 7 8; do \
	  docker exec audittrace-test-pg pg_isready -U postgres >/dev/null 2>&1 && break ; \
	  sleep 1 ; \
	done
	@AUDITTRACE_TEST_POSTGRES_URL="postgresql+psycopg2://postgres:test@localhost:15432/audittrace" \
	  .venv/bin/pytest tests/test_rls_isolation.py -v --no-cov ; \
	  status=$$? ; \
	  echo "🧹 Tearing down ephemeral test Postgres ..." ; \
	  docker rm -f audittrace-test-pg >/dev/null 2>&1 ; \
	  [ $$status -eq 0 ] || exit $$status
	@echo "✅ RLS integration tests passed (ephemeral Postgres, no production pollution)"

test-cov: ## Run tests with HTML coverage report + per-file gate
	@echo "🧪 Running tests with coverage..."
	@.venv/bin/pytest tests/ -v --cov=src --cov=scripts/deploy --cov=scripts/hooks --cov=scripts/release --cov=scripts/network --cov-report=html --cov-report=term-missing --cov-report=xml --cov-fail-under=90
	@echo "🔒 Enforcing per-file coverage gate (each component >= 90%)..."
	@.venv/bin/python scripts/check-per-file-coverage.py
	@echo "✅ Tests passed"
	@echo "📊 Open htmlcov/index.html to view coverage report"

test-coverage: test-cov ## Alias for test-cov

test-unit: ## Run unit tests only (fast)
	@echo "🧪 Running unit tests..."
	@.venv/bin/pytest tests/ -v -k "not integration" --cov=src --cov-report=term-missing
	@echo "✅ Unit tests passed"

test-watch: ## Run tests in watch mode
	@echo "🧪 Running tests in watch mode..."
	@.venv/bin/ptw --now . -- -v --cov=src --cov-report=term-missing

test-integration: docker-build ## Run RLS integration suite as a Helm test Pod inside the k8s cluster (Vault Agent + Istio mTLS path)
	# Builds + pushes the tests image (FROM the runtime image produced by
	# `docker-build`), then runs `helm test audittrace`. Pod runs through
	# the full production stack: Vault Agent injection, Istio mTLS,
	# in-cluster service DNS, audittrace-postgresql:5432.
	# WARNING: Postgres logs accumulate ERROR-by-design entries from the
	# RLS-violation test case. For pollution-free runs use `make test-rls-local`.
	@echo "🐳 Building tests image (FROM audittrace-ai:latest)..."
	@docker build -f Dockerfile.tests \
	  --build-arg TESTS_BASE_IMAGE=audittrace-ai:latest \
	  -t localhost:5000/audittrace/tests:latest . > /dev/null
	@echo "📦 Pushing to local registry..."
	@docker push localhost:5000/audittrace/tests:latest > /dev/null
	@echo "🧪 Running helm test (Vault + Istio mTLS path)..."
	@KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/config} helm test audittrace -n audittrace --logs
	@echo "✅ helm test integration suite passed"

clean: ## Clean up build artifacts
	@echo "🧹 Cleaning up..."
	@rm -rf .venv/
	@rm -rf build/
	@rm -rf dist/
	@rm -rf *.egg-info
	@rm -rf .pytest_cache/
	@rm -rf .mypy_cache/
	@rm -rf .ruff_cache/
	@rm -rf htmlcov/
	@rm -rf .coverage
	@echo "✅ Cleaned"

docker-build: ## Build Docker image
	@echo "🐳 Building Docker image..."
	@docker build -t audittrace-ai:latest .
	@echo "✅ Docker image built"

docker-run: ## Run Docker container
	@echo "🐳 Running container..."
	@docker run -p 8765:8765 --env-file .env audittrace-ai:latest

# ─────────────────── Kubernetes (k3s + Istio) ─────────────────────────

CHART_DIR   := charts/audittrace
RELEASE     := audittrace
NAMESPACE   := audittrace
VALUES_FILE := $(CHART_DIR)/values-local.yaml
# #394 fix: the laptop always runs the internal-registry image, never the
# chart-default docker.io/lfds ref. Pin the repository on every sanctioned
# helm mutation so a --reset-then-reuse-values reset cannot resurface the
# dockerhub default -> ImagePullBackOff. Single source for the literal.
LOCAL_MEMORY_REPO := localhost:5000/audittrace/memory-server

k8s-build: docker-build ## Build + push to local k3s registry. Honors TAG=... env var (default: latest). Use a unique TAG when rolling so k3s actually re-pulls instead of using the cached `:latest` digest.
	@_TAG="$${TAG:-latest}"; \
	docker tag audittrace-ai:latest localhost:5000/audittrace/memory-server:$$_TAG && \
	docker push localhost:5000/audittrace/memory-server:$$_TAG && \
	echo "pushed localhost:5000/audittrace/memory-server:$$_TAG"

k8s-deps: ## Update Helm chart dependencies (Bitnami subcharts)
	@helm dependency update $(CHART_DIR)

k8s-template: ## Render templates without installing (dry-run)
	@helm template $(RELEASE) $(CHART_DIR) -f $(VALUES_FILE) -n $(NAMESPACE)

helm-lint: ## Mirror the CI helm-lint job locally (both vault.enabled={false,true} blocks). Run before any chart commit.
	@echo "🪖  helm lint (vault.enabled=false)..."
	@helm lint $(CHART_DIR) \
	  --set vault.enabled=false \
	  --set secrets.minio.secretKey=ci-test \
	  --set secrets.minio.kmsKey=ci-test \
	  --set secrets.chromadb.token=ci-test \
	  --set secrets.keycloak.adminPassword=ci-test \
	  --set secrets.postgres.appPassword=ci-test \
	  --set secrets.postgres.password=ci-test \
	  --set secrets.redis.password=ci-test \
	  --set secrets.summariser.password=ci-test \
	  --set externalLLM.host=llm.test.invalid \
	  --set observability.external.langfuseHost=langfuse.test.invalid \
	  --set observability.external.tempoHost=tempo.test.invalid \
	  --set observability.external.lokiHost=loki.test.invalid
	@echo "🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count..."
	@helm template $(RELEASE) $(CHART_DIR) -n $(NAMESPACE) \
	  --set vault.enabled=true \
	  --set secrets.minio.secretKey=ci-test \
	  --set secrets.minio.kmsKey=ci-test \
	  --set secrets.chromadb.token=ci-test \
	  --set secrets.keycloak.adminPassword=ci-test \
	  --set secrets.postgres.appPassword=ci-test \
	  --set secrets.postgres.password=ci-test \
	  --set secrets.redis.password=ci-test \
	  --set secrets.summariser.password=ci-test \
	  --set externalLLM.host=llm.test.invalid \
	  --set observability.external.langfuseHost=langfuse.test.invalid \
	  --set observability.external.tempoHost=tempo.test.invalid \
	  --set observability.external.lokiHost=loki.test.invalid \
	  > /tmp/audittrace-helm-rendered.yaml
	@guards=$$(grep -c "Vault Agent did not inject /vault/secrets/env (exit 79)" /tmp/audittrace-helm-rendered.yaml || echo 0); \
	  if [ "$$guards" -lt 3 ]; then \
	    echo "❌ expected >=3 vaultSecretFileGuard occurrences, found $$guards"; \
	    exit 1; \
	  fi; \
	  echo "✅ vaultSecretFileGuard present in $$guards workloads"

sync-dashboards: ## SPEC #441 SSOT: reconcile $(OBS_STACK_DIR)/grafana/dashboards/ to the chart's canonical set — copy new, update drifted, prune stale (pre-rename `sovereign-*` etc.), chmod 0644 (Grafana file-provider perms). Idempotent; prints added/updated/pruned. Skips gracefully (exit 0) when the obs-stack dir is absent.
	@SRC=$(CHART_DIR)/files/grafana-dashboards; \
	if [ ! -d "$(OBS_STACK_DIR)" ]; then \
	  echo "obs-stack not present, skipping ($(OBS_STACK_DIR))"; \
	  exit 0; \
	fi; \
	DEST="$(OBS_STACK_DIR)/grafana/dashboards"; \
	mkdir -p "$$DEST"; \
	added=""; updated=""; \
	for f in "$$SRC"/*.json; do \
	  [ -e "$$f" ] || continue; \
	  base=$$(basename "$$f"); \
	  if cmp -s "$$f" "$$DEST/$$base"; then :; \
	  elif [ -e "$$DEST/$$base" ]; then updated="$$updated $$base"; \
	  else added="$$added $$base"; fi; \
	  cp "$$f" "$$DEST/$$base" && chmod 0644 "$$DEST/$$base"; \
	done; \
	pruned=""; \
	for f in "$$DEST"/*.json; do \
	  [ -e "$$f" ] || continue; \
	  base=$$(basename "$$f"); \
	  if [ ! -e "$$SRC/$$base" ]; then \
	    rm -f "$$f"; \
	    pruned="$$pruned $$base"; \
	  fi; \
	done; \
	echo "sync-dashboards: $$SRC -> $$DEST"; \
	echo "  added:   $$added"; \
	echo "  updated: $$updated"; \
	echo "  pruned:  $$pruned"; \
	if [ -z "$$added$$updated$$pruned" ]; then echo "  (already in sync — no changes)"; fi

check-dashboard-drift: ## SPEC #441 drift guard: $(OBS_STACK_DIR)/grafana/dashboards/ must be an EXACT mirror of the chart's canonical set (file set AND content). exit 1 with a clear message on ANY divergence, exit 0 when identical. Skips gracefully (exit 0) when the obs-stack dir is absent (CI-safe).
	@SRC=$(CHART_DIR)/files/grafana-dashboards; \
	if [ ! -d "$(OBS_STACK_DIR)" ]; then \
	  echo "obs-stack not present, skipping dashboard drift check ($(OBS_STACK_DIR))"; \
	  exit 0; \
	fi; \
	DEST="$(OBS_STACK_DIR)/grafana/dashboards"; \
	if [ ! -d "$$DEST" ]; then \
	  echo "dashboard drift: $$DEST does not exist — run 'make sync-dashboards'"; \
	  exit 1; \
	fi; \
	status=0; \
	for f in "$$SRC"/*.json; do \
	  [ -e "$$f" ] || continue; \
	  base=$$(basename "$$f"); \
	  if [ ! -e "$$DEST/$$base" ]; then \
	    echo "dashboard drift: MISSING in obs-stack: $$base"; \
	    status=1; \
	  elif ! cmp -s "$$f" "$$DEST/$$base"; then \
	    echo "dashboard drift: CONTENT differs: $$base (chart vs $$DEST)"; \
	    status=1; \
	  fi; \
	done; \
	for f in "$$DEST"/*.json; do \
	  [ -e "$$f" ] || continue; \
	  base=$$(basename "$$f"); \
	  if [ ! -e "$$SRC/$$base" ]; then \
	    echo "dashboard drift: EXTRA in obs-stack (not in chart canonical set): $$base"; \
	    status=1; \
	  fi; \
	done; \
	if [ "$$status" -eq 0 ]; then \
	  echo "no dashboard drift: $$DEST == $$SRC (file set + content)"; \
	fi; \
	exit $$status

deploy-preflight: ## Pre-deploy gate: helm lint + template + kubectl dry-run + Vault injector probe (REQUIRED before any cluster mutation)
	@TAG="$(TAG)" CHART_DIR=$(CHART_DIR) RELEASE=$(RELEASE) NAMESPACE=$(NAMESPACE) \
	  scripts/deploy-preflight.sh

verify-deploy: ## Post-deploy gate: pods Ready, helm status deployed, /health, /metrics, pg_isready, Tempo traces, Loki ERROR threshold (Phase C.12)
	@RELEASE=$(RELEASE) NAMESPACE=$(NAMESPACE) scripts/post-deploy-verify.sh

k8s-bootstrap-secrets: ## Post-helm bootstrap: Vault provisioning + Keycloak memory scopes (idempotent; run after every helm install/upgrade that touches operator-bound infra). Requires VAULT_TOKEN exported. SECRETS_DIR defaults to ~/work/audittrace-private/secrets/ — override to point elsewhere.
	@SECRETS_DIR=$${SECRETS_DIR:-$$HOME/work/audittrace-private/secrets} scripts/setup-vault.sh
	@scripts/setup-memory-scopes.sh

sync-requirements: ## Regenerate requirements.txt from pyproject.toml (single source of truth). Run after touching dependencies; the requirements-sync hook + CI job block drifted state.
	@python3 scripts/sync-requirements.py

check-requirements-sync: ## Fail if requirements.txt has drifted from pyproject.toml (mirrors the pre-commit hook + CI job).
	@python3 scripts/sync-requirements.py --check

check-pr-body: ## Validate a PR-body file against the ADR-049 evidence gate (local mirror of CI). Usage: make check-pr-body BODY=scratchpad/PR-BODY-x.md
	@if [ -z "$(BODY)" ]; then \
		echo "❌ usage: make check-pr-body BODY=<path-to-pr-body.md>"; \
		echo "   Runs the SAME parser as the CI evidence-check job"; \
		echo "   (scripts/adr049_evidence_check.py) so a failing PR body"; \
		echo "   is caught locally BEFORE push, not after (#396)."; \
		exit 1; \
	fi
	@python3 scripts/adr049_evidence_check.py "$(BODY)"

openapi-export: ## Regenerate docs/reference/audittrace/openapi.yaml + tests/fixtures/openapi.snapshot.yaml from the live FastAPI app (ADR-046 / v1.0.10).
	@OPENAPI_SNAPSHOT_UPDATE=1 .venv/bin/pytest tests/test_openapi_drift.py -q --no-cov
	@echo "✅ Regenerated:"
	@echo "   tests/fixtures/openapi.snapshot.yaml"
	@echo "   docs/reference/audittrace/openapi.yaml"
	@echo "Commit both alongside the API change so reviewers see the diff."

release: ## Bump pyproject + Chart.yaml::appVersion to VERSION + regenerate OpenAPI snapshot + run drift gate. Usage: make release VERSION=1.0.14. (ADR-055)
	@if [ -z "$(VERSION)" ]; then \
		echo "❌ usage: make release VERSION=1.0.14"; \
		echo "Bumps the two single-source-of-truth pin sites + regenerates"; \
		echo "the OpenAPI snapshot. Stops short of committing or tagging —"; \
		echo "review the diff, then commit + tag manually."; \
		exit 1; \
	fi
	@echo "🔖 bumping pyproject.toml::version → $(VERSION)"
	@sed -i 's/^version = ".*"/version = "$(VERSION)"/' pyproject.toml
	@echo "🔖 bumping charts/audittrace/Chart.yaml::version + appVersion → $(VERSION)"
	@sed -i 's/^version: .*/version: $(VERSION)/' charts/audittrace/Chart.yaml
	@sed -i 's/^appVersion: ".*"/appVersion: "$(VERSION)"/' charts/audittrace/Chart.yaml
	@echo "🔖 bumping docker-compose.yml demo default → $(VERSION)"
	@sed -i 's/$${AUDITTRACE_IMAGE_TAG:-[^}]*}/$${AUDITTRACE_IMAGE_TAG:-$(VERSION)}/g' docker-compose.yml
	@echo "🔖 bumping .env.ci + .env.dev-real-llm.example AUDITTRACE_IMAGE_TAG → $(VERSION)"
	@sed -i 's/^AUDITTRACE_IMAGE_TAG=.*/AUDITTRACE_IMAGE_TAG=$(VERSION)/' .env.ci .env.dev-real-llm.example
	@echo "📝 regenerating OpenAPI snapshot ..."
	@OPENAPI_SNAPSHOT_UPDATE=1 .venv/bin/pytest tests/test_openapi_drift.py -q --no-cov >/dev/null
	@echo "🚦 running drift gate ..."
	@.venv/bin/pytest tests/test_version_drift.py -q --no-cov
	@echo
	@echo "✅ release-prep done for v$(VERSION). Diff:"
	@git diff --stat pyproject.toml charts/audittrace/Chart.yaml \
	    docs/reference/audittrace/openapi.yaml tests/fixtures/openapi.snapshot.yaml README.md \
	    docker-compose.yml .env.ci .env.dev-real-llm.example
	@echo
	@echo "Next steps:"
	@echo "  1. git add pyproject.toml charts/audittrace/Chart.yaml \\"
	@echo "         docs/reference/audittrace/openapi.yaml \\"
	@echo "         tests/fixtures/openapi.snapshot.yaml \\"
	@echo "         docker-compose.yml .env.ci .env.dev-real-llm.example"
	@echo "  2. git commit -m 'chore(release): v$(VERSION)'"
	@echo "  3. open release PR; after merge, tag v$(VERSION) on main"
	@echo "  4. docker build/push localhost:5000/audittrace/memory-server:v$(VERSION)"
	@echo "  5. helm upgrade --reset-then-reuse-values --set memoryServer.image.tag=v$(VERSION)"

release-cut: ## Deterministic release CUT via the runner (#398): bump+gate+branch+push; NEVER tags. Usage: make release-cut VERSION=1.0.14 [DRY_RUN=1]
	@python -m scripts.release.runner --version $(VERSION) $(if $(DRY_RUN),--dry-run,)

k8s-install: k8s-deps deploy-preflight ## Install the Helm chart on k3s (gated by preflight; pins the internal image repository — #394)
	@kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	@kubectl label namespace $(NAMESPACE) istio-injection=enabled --overwrite
	@helm install $(RELEASE) $(CHART_DIR) -f $(VALUES_FILE) -n $(NAMESPACE) \
	  --set memoryServer.image.repository=$(LOCAL_MEMORY_REPO)

k8s-upgrade: deploy-preflight ## Upgrade the Helm release with values file (gated by preflight; pins the internal image repository — #394)
	@helm upgrade $(RELEASE) $(CHART_DIR) -f $(VALUES_FILE) -n $(NAMESPACE) \
	  --set memoryServer.image.repository=$(LOCAL_MEMORY_REPO)

k8s-rolling-image: ## Image-only roll via the CD runner (preflight + repo-pin + mesh-gate; folds #394; #384 WS4b)
	@# WS4b: delegate the image roll to the CD runner (scripts.deploy.runner), which
	@# already runs deploy-preflight.sh itself (phase_preflight), pins the internal
	@# repository via --registry local (closing the #394 footgun), runs the mesh gate,
	@# and emits a non-self-certifying report. No Make-level deploy-preflight prereq here:
	@# the runner owns preflight, so keeping it would run preflight twice.
	@# Contract (#451): the runner bounds helm via --timeout (default 300s) itself —
	@# callers MUST NOT wrap this target in a short outer `timeout`; that truncation
	@# is what filed #451 (a killed helm apply left the release in a stuck state).
	@python -m scripts.deploy.runner \
	  --target-version $(TAG) \
	  --release $(RELEASE) \
	  --namespace $(NAMESPACE) \
	  --registry local

k8s-status: ## Show pod/service/Istio status
	@echo "=== Pods ==="
	@kubectl get pods -n $(NAMESPACE) -o wide
	@echo "\n=== Services ==="
	@kubectl get svc -n $(NAMESPACE)
	@echo "\n=== Istio VirtualServices ==="
	@kubectl get virtualservices -n $(NAMESPACE) 2>/dev/null || true
	@echo "\n=== Istio PeerAuthentication ==="
	@kubectl get peerauthentication -n $(NAMESPACE) 2>/dev/null || true
