# ADR-065: Hashed dependency locks; install only from them

- **Status:** Accepted (2026-10-19, operator rulings Q1-Q5, C7, C8; spec gate RATIFIED-WITH-CONDITIONS C1-C10)
- **Unit:** DEP-LOCK-AT, WU-1 (locks + install paths). WU-2 (image build and live evidence) is separate.
- **Supersedes:** the range-based `requirements.txt` rendered from `pyproject.toml` by `scripts/sync-requirements.py` (ADR-055's companion hook), and the unpinned `pip install -e ".[dev]"` install paths.

## Context

Every Python dependency was a floor (`>=`) or a loose range, and `requirements.txt` was a byte-for-byte render of those ranges with zero hashes. CI, the laptop venvs, the pre-commit mypy environments and the image each resolved PyPI on their own day. On 2026-10-18 four fastapi versions (0.140.7, 0.141.1, 0.143.0, 0.146.0) were live on one machine, a venv ran opentelemetry-api 1.44.0 against its own `>=1.45.0,<1.46.0` pin, and PR #395's type check broke on a fastapi release CI picked up and the laptop had not.

Operator policy (2026-10-18): pin every dependency; upgrade only on purpose.

## Decision

1. **Direct dependencies are pinned `==`** in `pyproject.toml` (runtime, `dev`, `gate`, and `[build-system].requires`).
2. **Hashed locks are compiled with `uv pip compile --universal --generate-hashes --allow-unsafe`** (Q1: uv over pip-tools, measured 1063 s / 821 MiB unfinished for pip-tools on this set; uv 2.3 s). Five locks, one matrix in `scripts/lock.sh`:

   | lock | inputs | installed by |
   |---|---|---|
   | `requirements.txt` (runtime) | `pyproject.toml`, `requirements-build.in` | `Dockerfile` builder |
   | `requirements-dev.txt` | + `--extra dev`, `-c requirements.txt` | `make venv`, CI, `setup.sh`, `Dockerfile.tests` |
   | `bff/requirements.txt` | `bff/requirements.in`, `-c requirements.txt` | `bff/Dockerfile` |
   | `images/llm-stub/requirements.txt` | `images/llm-stub/requirements.in` | `images/llm-stub/Dockerfile` |
   | `requirements-gate.txt` (C7) | `requirements-gate.in` | `make gate-venv` (isolated semgrep venv) |

   `uv`, `pip` (Q5), `setuptools` and `wheel` (C1) are pinned inside the locks with hashes.
3. **One install chokepoint, `scripts/install-locked.sh`:** pip from its hashed line (never `--upgrade pip`); `--require-hashes --only-binary=:all: -r <lock>`; then `--no-deps --no-build-isolation` for `-e .` and the VCS package. `--no-build-isolation` closes the unhashed build-backend fetch (B1).
4. **The VCS dependency** `audittrace-object-storage` cannot be hashed (pip refuses VCS requirements in hash mode). It is pinned to the full commit SHA of tag `v0.1.1` (`cc21c55...`) in `pyproject.toml` only; every other file reads that SHA. It is omitted from the locks (`--no-emit-package`). WU-0 (Q2): publish a wheel asset, fold it into the lock, delete this carve-out.
5. **Gates:**
   - G1 `make lock-check` (own CI job, a REQUIRED check on `main`, Q4): each committed lock is the fixed point of its inputs under its own pinned versions: the lock is reduced to a versions-only constraints file and the inputs are compiled fresh, so every hash is re-derived from the index (comment-stripped diff). A SEEDED recompile was measured to be blind to a tampered hash (uv trusts hashes already in the output file; neuter N1 stayed green), hence the fresh compile. Residual: PyPI adding a file to an already-locked release turns the check RED until `make lock`.
   - G2 `lock-freshness.yml`: weekly, informational, never required.
   - G3 `tests/test_dep_lock_sync.py`: every direct pin is in its lock at that version with hashes in its own block.
   - G4 `tests/test_dep_lock_install_paths.py`: every `pip install` in a tracked file is hashed + wheel-only, or an exempt `--no-deps --no-build-isolation` build of exactly `.` / the VCS requirement; no `uvx`, `pipx`, `uv pip install`; one VCS SHA.
   - G5 `scripts/ci/check_venv_matches_lock.py`, called by `scripts/ci/run-tests.sh` before pytest, fail closed, no bypass: the interpreter running the suite is exactly the dev lock.
   - G6 `tests/test_python_pin_sites.py`: the 15 Python pin sites and the five lock headers name one Python.
6. **Pre-commit** runs ruff and mypy as `repo: local` hooks through `make lint-ruff` / `make typecheck`, so the hook and `make lint` use the same lock-pinned tools. Autofix is dropped from the hook (check-only); `make format` fixes.
7. **Semgrep** (C7): its own hashed gate lock into an isolated `.gate-venv` (`make gate-venv`); `make security-lint` accepts no semgrep from anywhere else.
8. **Upgrades are deliberate:** `make lock-upgrade [PKG=name]`, then the full gates, then a PR whose body names the pin diff. Dependabot (`/` only) bumps direct pins in `pyproject.toml`; `lock-check` then fails until the locks are recompiled. `bff/requirements.in`, `images/llm-stub/requirements.in`, `requirements-build.in` and `requirements-gate.in` move only through `make lock-upgrade` (C8). Whether Dependabot regenerates uv-style hashed files is verified on its first post-merge PR (Q3).

## Baseline (spec gate A2)

The first lock is a deliberate mass upgrade of every laptop venv. Direct pins at WU-1: fastapi 0.146.0, starlette 1.9.0, langfuse 4.18.0, opentelemetry-api/sdk 1.45.1, opentelemetry-instrumentation-fastapi 0.66b1, sqlalchemy 2.1.5, pydantic 2.14.1, chromadb 1.5.9, mcp 1.30.0, pyhanko 0.37.0, mypy 2.4.0, ruff 0.17.1. Two pins are held at the spec's measured baseline instead of PyPI's newest on the day: fastapi at 0.146.0 (0.148.0 was already published) and pymupdf at 1.28.2 (2.0 is a new major that also pulls pymupdf4llm, pymupdf-layout and networkx). WU-2 diffs `pip freeze` of the deployed image against `requirements.txt`.

## Consequences

- A venv that drifts from `requirements-dev.txt` is refused by `make test` (G5). Recovery: `rm -rf .venv && make venv`. Every existing worktree venv must be rebuilt this way.
- `make security-lint` and `make lint` need `make gate-venv` once per checkout (or `GATE_VENV=` pointing at a shared one).
- The base-image pip in `python:3.12-slim` floats with the base tag; digest-pinning base images is out of scope (gate A6).
- The arm64 publish leg installs from the same universal lock; pre-merge proof is a `pip download --platform manylinux_2_36_aarch64` (AC-7a), the real image is post-tag evidence (AC-7, WU-2).
