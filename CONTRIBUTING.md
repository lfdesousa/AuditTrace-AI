# Contributing to AuditTrace-AI

Thank you for your interest in contributing.

## Development Setup

```bash
git clone https://github.com/lfdesousa/AuditTrace-AI
cd AuditTrace-AI
./scripts/setup.sh   # or: make install
```

## Code Style

- **Linter:** `ruff check src/ tests/`
- **Formatter:** `ruff format src/ tests/`
- **Type checking:** `mypy src/`
- Line length: 88 characters
- Python 3.12+ features encouraged (type unions, f-strings, pathlib)

## Testing

- **Framework:** pytest
- **Coverage floor:** 90% (enforced in CI)
- **Run:** `make test`
- All new code must include tests
- Follow existing ABC + implementation + mock pattern

## Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>

Types: feat, fix, docs, refactor, test, ci, chore, perf
Scopes: db, auth, infra, memory, routes, config
```

## Pull Request Workflow

1. Fork the repository
2. Create a feature branch from `main`
3. Write tests first (TDD)
4. Implement the feature
5. Ensure all checks pass: `ruff check && ruff format --check && pytest`
6. Submit a PR with a clear description

## Architecture Decision Records

Significant changes require an ADR in `docs/ADR-NNN-kebab-title.md`.

## License

By contributing, you agree that your contributions will be licensed under the AGPL-3.0 license.
