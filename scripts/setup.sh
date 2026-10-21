#!/bin/bash
# First-time setup script for audittrace-ai
# Creates isolated virtual environment - NO system-wide installations

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "🚀 Setting up audittrace-ai..."
echo ""

# Check Python version
echo "🐍 Checking Python version..."
PYTHON_VERSION=$(python3 --version 2>&1 | cut -d' ' -f2)
echo "   Python $PYTHON_VERSION"

# Create virtual environment (isolated from system)
if [ ! -d ".venv" ]; then
    echo ""
    echo "📦 Creating isolated virtual environment (.venv)..."
    python3 -m venv .venv
    echo "   ✅ Virtual environment created at .venv/"
else
    echo "   ✅ Virtual environment already exists"
fi

# Install dependencies from the HASHED lock (DEP-LOCK-AT, ADR-065): pip from
# its hashed line, then --require-hashes --only-binary=:all: -r
# requirements-dev.txt, then the project (-e .) and the VCS package, all
# --no-deps --no-build-isolation. No `pip install --upgrade pip`, no range
# resolution. To rebuild a drifted venv: rm -rf .venv && make venv
echo ""
echo "📦 Installing project dependencies from the hashed lock..."
bash scripts/install-locked.sh .venv/bin/pip

# Install pre-commit hooks
echo ""
echo "🔗 Installing pre-commit hooks..."
.venv/bin/pre-commit install

# Copy environment file
if [ ! -f ".env" ]; then
    echo ""
    echo "📝 Creating .env from .env.example..."
    cp .env.example .env
fi

echo ""
echo "=========================================="
echo "✅ Setup complete!"
echo "=========================================="
echo ""
echo "📁 Virtual environment: .venv/"
echo "📦 Dependencies installed"
echo "🔗 Pre-commit hooks installed"
echo ""
echo "👉 Next steps:"
echo "   1. Edit .env with your configuration"
echo "   2. Run tests: make test"
echo "   3. Run linting: make lint"
echo "   4. Start server: uvicorn audittrace.server:app --reload"
echo ""
echo "💡 Tip: Use 'make help' to see all available commands"
