.PHONY: help venv install lint test test-cov test-coverage clean \
       docker-build docker-run k8s-build k8s-install k8s-upgrade k8s-status k8s-template

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
	@echo ""
	@echo "Run tests: make test"

lint: ## Run linting and formatting
	@echo "🔍 Running linter..."
	@.venv/bin/ruff check src/ tests/
	@echo "✅ Linting passed"
	@echo "📝 Running formatter check..."
	@.venv/bin/ruff format --check src/ tests/
	@echo "✅ Formatting passed"

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
	@.venv/bin/pytest tests/ -v --cov=src --cov-report=term-missing --cov-report=xml --cov-fail-under=90 --junit-xml=junit.xml
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
	@.venv/bin/pytest tests/ -v --cov=src --cov-report=html --cov-report=term-missing --cov-report=xml --cov-fail-under=90
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

k8s-build: docker-build ## Build + push to local k3s registry
	@docker tag audittrace-ai:latest localhost:5000/audittrace/memory-server:latest
	@docker push localhost:5000/audittrace/memory-server:latest
	@echo "pushed to localhost:5000"

k8s-deps: ## Update Helm chart dependencies (Bitnami subcharts)
	@helm dependency update $(CHART_DIR)

k8s-template: ## Render templates without installing (dry-run)
	@helm template $(RELEASE) $(CHART_DIR) -f $(VALUES_FILE) -n $(NAMESPACE)

k8s-install: k8s-deps ## Install the Helm chart on k3s
	@kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	@kubectl label namespace $(NAMESPACE) istio-injection=enabled --overwrite
	@helm install $(RELEASE) $(CHART_DIR) -f $(VALUES_FILE) -n $(NAMESPACE)

k8s-upgrade: ## Upgrade the Helm release
	@helm upgrade $(RELEASE) $(CHART_DIR) -f $(VALUES_FILE) -n $(NAMESPACE)

k8s-status: ## Show pod/service/Istio status
	@echo "=== Pods ==="
	@kubectl get pods -n $(NAMESPACE) -o wide
	@echo "\n=== Services ==="
	@kubectl get svc -n $(NAMESPACE)
	@echo "\n=== Istio VirtualServices ==="
	@kubectl get virtualservices -n $(NAMESPACE) 2>/dev/null || true
	@echo "\n=== Istio PeerAuthentication ==="
	@kubectl get peerauthentication -n $(NAMESPACE) 2>/dev/null || true
