.PHONY: help venv install lint test test-cov test-coverage clean

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
	@.venv/bin/pytest tests/ -v --cov=src --cov-report=term-missing --cov-report=xml --cov-fail-under=90
	@echo "🔒 Enforcing per-file coverage gate (each component >= 90%)..."
	@.venv/bin/python scripts/check-per-file-coverage.py
	@echo "✅ Tests passed"

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
