"""G4-CP allow-list DATA (ADDENDUM-2 section 4/5; fix round 3).

Hand-edited and reviewed as a diff. NOTHING in the repo regenerates it from the tree: a
guard cannot validate an allow list it wrote itself. Every entry is the EXACT joined
logical line (backslash-newline and the next line's leading blanks deleted) of its file.
Hygiene (S4'): an entry must equal exactly one unit of its file (0 = stale, 2+ =
duplicate) and must itself carry an installer token.
"""

from __future__ import annotations

# Install / provision / compile statements, and the COPY / run lines that name a lock file.
SANCTIONED_STATEMENTS: frozenset[tuple[str, str]] = frozenset(
    {
        (".github/workflows/ci.yml", "        run: bash scripts/install-locked.sh pip"),
        (
            ".github/workflows/ci.yml",
            "        run: bash scripts/install-locked.sh pip uv-only",
        ),
        (
            ".github/workflows/lock-freshness.yml",
            "        run: bash scripts/install-locked.sh pip uv-only",
        ),
        (
            ".github/workflows/test-count-badge.yml",
            "        run: bash scripts/install-locked.sh pip",
        ),
        ("Dockerfile", "COPY requirements.txt ."),
        (
            "Dockerfile",
            "RUN pip install --user --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt",
        ),
        (
            "Dockerfile",
            'RUN SHA="$(sed -n \'s#.*audittrace-object-storage\\.git@\\([0-9a-f]\\{40\\}\\)".*#\\1#p\' pyproject.toml)" && test -n "$SHA" && pip install --user --no-cache-dir --no-deps --no-build-isolation "audittrace-object-storage @ git+https://github.com/lfdesousa/audittrace-object-storage.git@${SHA}"',
        ),
        (
            "Dockerfile",
            "RUN pip install --user --no-cache-dir --no-deps --no-build-isolation .",
        ),
        ("Dockerfile.tests", "COPY requirements-dev.txt /tmp/requirements-dev.txt"),
        (
            "Dockerfile.tests",
            "RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements-dev.txt && rm /tmp/requirements-dev.txt",
        ),
        ("Makefile", "\t@bash scripts/install-locked.sh .venv/bin/pip"),
        ("Makefile", "\t@bash scripts/install-locked.sh $(GATE_VENV)/bin/pip gate"),
        ("bff/Dockerfile", "COPY bff/requirements.txt ./requirements.txt"),
        (
            "bff/Dockerfile",
            "RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt",
        ),
        ("images/llm-stub/Dockerfile", "COPY requirements.txt /app/requirements.txt"),
        (
            "images/llm-stub/Dockerfile",
            "RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /app/requirements.txt",
        ),
        # WU-2 (AC-6/G5). Read-only freeze listings and lock-name constants: nothing here installs.
        ("scripts/ci/run-tests.sh", "DEV_LOCK=requirements-dev.txt"),
        ("scripts/ci/run-tests.sh", "GATE_LOCK=requirements-gate.txt"),
        (
            "scripts/ci/check-images-match-locks.sh",
            '  docker run --rm ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} --entrypoint python "$image" -c \'import json; from pip._vendor.packaging.markers import default_environment as d; print(json.dumps(d()))\' > "$SCRATCH/$label.markers.json"',
        ),
        (
            "scripts/ci/check-images-match-locks.sh",
            '  docker run --rm ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} --entrypoint python "$image" -m pip freeze --all > "$SCRATCH/$label.freeze"',
        ),
        (
            "scripts/ci/check-images-match-locks.sh",
            'check memory-server "$MS" requirements.txt --allow-extra audittrace-ai --allow-extra audittrace-object-storage --vcs-package audittrace-object-storage --pyproject pyproject.toml || RC=1',
        ),
        (
            "scripts/ci/check-images-match-locks.sh",
            'check bff "$BFF" bff/requirements.txt || RC=1',
        ),
        (
            "scripts/ci/check-images-match-locks.sh",
            "BASE_IMAGE_TOOLING=(--allow-extra pip)",
        ),
        (
            "scripts/ci/check-images-match-locks.sh",
            'check llm-stub "$STUB" images/llm-stub/requirements.txt || RC=1',
        ),
        (
            "scripts/ci/check_freeze_matches_lock.py",
            '            [python, "-m", "pip", "freeze", "--all"],',
        ),
        (
            "scripts/ci/check_venv_matches_lock.py",
            "    from pip._vendor.packaging.markers import (  # type: ignore[no-redef]",
        ),
        (
            "scripts/ci/check_venv_matches_lock.py",
            "    from pip._vendor.packaging.utils import (  # type: ignore[no-redef]",
        ),
        ("scripts/setup.sh", "bash scripts/install-locked.sh .venv/bin/pip"),
    }
)

# Configuration, help strings, an eval prompt, a docstring: lines that carry a token but run nothing.
ALLOWED_CONFIGURATION: frozenset[tuple[str, str]] = frozenset(
    {
        (".github/dependabot.yml", '  - package-ecosystem: "pip"'),
        (".github/workflows/ci.yml", "      - name: Cache pip dependencies"),
        (".github/workflows/ci.yml", "          path: ~/.cache/pip"),
        (
            ".github/workflows/ci.yml",
            "          key: ${{ runner.os }}-pip-${{ hashFiles('requirements-dev.txt', 'requirements-gate.txt') }}",
        ),
        (".github/workflows/ci.yml", "            ${{ runner.os }}-pip-"),
        (".github/workflows/ci.yml", "      - name: Provision uv from the hashed lock"),
        (
            ".github/workflows/lock-freshness.yml",
            "      - name: Provision uv from the hashed lock",
        ),
        (".gitignore", ".tox/"),
        (
            "scripts/ci/check_venv_matches_lock.py",
            "drifted from ``requirements-dev.txt`` (the 2026-10-18 failure: a venv with",
        ),
        (
            "scripts/ci/check_venv_matches_lock.py",
            '        "lock", type=Path, help="the hashed lock, e.g. requirements-dev.txt"',
        ),
        (
            "scripts/eval-memory-modes.py",
            '        "How do I regenerate requirements.txt after editing pyproject.toml?",',
        ),
        (
            "src/audittrace/telemetry.py",
            "    requirements.txt) to the minor this was tested against protects CI;",
        ),
    }
)

SANCTIONED: frozenset[tuple[str, str]] = SANCTIONED_STATEMENTS | ALLOWED_CONFIGURATION

# Chokepoint scripts: byte-identical to their pin, or RED until the pin is updated on purpose.
HASH_PINNED: dict[str, str] = {
    "scripts/install-locked.sh": "2e6ceb154ee09f21f952920eefc7cc29a27a2209b3f1a2da34a4536e8bd47e1c",
    "scripts/lock.sh": "d24954bd611d3908fddf33250a6a0307fb6263ab1721ff41d476737fd430fe93",
}

# Undecodable files that are expected: 4 kinds (.der, .meta, .pdf; the .tgz are pinned below).
BINARIES: frozenset[str] = frozenset(
    [
        "charts/audittrace/trust-store/swiss-federal-tsl/CH-TL-cert.der",
        "main_signed.pdf",
        "memory/episodic/ADR-014-python-package-structure.md/xl.meta",
        "memory/episodic/ADR-014.2-logging-dependency-injection.md/xl.meta",
        "memory/episodic/ADR-014.3-makefile-venv.md/xl.meta",
        "memory/episodic/ADR-014.4-observability-logging-otel.md/xl.meta",
        "memory/episodic/ADR-018-four-layer-memory-port.md/xl.meta",
        "memory/episodic/ADR-019-chromadb-server-mode.md/xl.meta",
        "memory/episodic/ADR-020-postgresql-server-databases.md/xl.meta",
        "memory/episodic/ADR-021-tls-mkcert-traefik.md/xl.meta",
        "memory/episodic/ADR-021.2-langfuse-sibling-stack.md/xl.meta",
        "memory/episodic/ADR-022-keycloak-realm.md/xl.meta",
        "memory/episodic/ADR-023-jwt-validation-jwks-caching.md/xl.meta",
        "memory/episodic/ADR-024-proxy-passthrough-and-langfuse-trace-decoupling.md/xl.meta",
        "memory/episodic/ADR-025-memory-as-tools.md/xl.meta",
        "memory/episodic/ADR-026-multi-user-identity.md/xl.meta",
        "memory/episodic/ADR-027-minio-object-storage.md/xl.meta",
    ]
)

# Archives pinned by path AND sha256. Any other *.tar*, *.tgz, *.zip, *.whl is RED.
ARCHIVES: dict[str, str] = {
    "charts/audittrace/charts/postgresql-16.7.27.tgz": "d7d0f7d0342d56504f3bb2f77867e0b7173f6322f11266b74e2d0eda53365901",
    "charts/audittrace/charts/rabbitmq-14.7.0.tgz": "83447aa07801a8a84eab6d3216e45d5f962709e5fdd6be5db50f71d99167564c",
    "charts/audittrace/charts/redis-19.6.4.tgz": "488b39d7dbf107c3749aedb11fd76a8be04762de91920d1b607590ffdd99bd9d",
    "charts/audittrace/charts/vault-0.30.1.tgz": "a52b3b16aa9afae2deeb3d63500578a62c3daa777551f2c5eb6c2cd07bfd79cc",
}
