"""Hermetic drift guard for the sanctioned deploy paths in the ``Makefile``.

#384 WS4b converged the laptop deploy paths so that *every* helm mutation the
repo sanctions either delegates to the CD runner (which runs preflight +
mesh-gate and pins the internal image repository) or explicitly pins that
repository itself. This test mechanically enforces that invariant against the
``Makefile`` text so the divergence that caused the #394 ``ImagePullBackOff``
footgun cannot silently return.

The test is deliberately hermetic: it parses the ``Makefile`` string only. No
cluster, no subprocess, no shell-out to ``helm``.

Two invariants are asserted:

1. **Hot path converged** — the ``k8s-rolling-image`` recipe contains no
   ``helm upgrade`` / ``helm install`` command and *does* invoke
   ``scripts.deploy.runner`` with ``--registry local`` (without which the
   runner would resolve the Docker Hub default and re-open #394).
2. **Every residual helm mutation pins the repo** — any recipe command that
   actually runs ``helm upgrade`` or ``helm install`` must set
   ``memoryServer.image.repository=`` so no sanctioned path can omit the pin.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MAKEFILE_PATH = REPO_ROOT / "Makefile"

# A recipe "command" may run helm as its first token after the recipe-prefix
# characters (@ silences echo, - ignores errors, + forces execution). Echoed
# help text (``@echo "... helm upgrade ..."``) must NOT match — only commands
# that actually invoke helm.
_HELM_MUTATION_RE = re.compile(r"^helm\s+(?:upgrade|install)\b")
_RECIPE_PREFIX_RE = re.compile(r"^[@\-+]+")
# A target header: starts in column 0, has a colon, and is not a ``:=``/``::=``
# / ``?=`` / ``+=`` variable assignment.
_TARGET_HEADER_RE = re.compile(r"^(?P<name>[A-Za-z0-9_./%-]+)\s*::?=?")


def _parse_recipes(makefile_text: str) -> dict[str, list[str]]:
    """Parse ``Makefile`` text into ``{target: [logical recipe command, ...]}``.

    Recipe lines are tab-indented. Trailing-backslash continuations are joined
    into a single logical command so a ``--set`` on a continuation line is
    considered part of the command it continues.
    """

    recipes: dict[str, list[str]] = {}
    current: str | None = None
    pending: list[str] = []

    def _flush() -> None:
        if current is not None and pending:
            joined = " ".join(part.rstrip("\\").strip() for part in pending)
            recipes.setdefault(current, []).append(joined)
        pending.clear()

    continuing = False
    for raw in makefile_text.splitlines():
        if raw.startswith("\t"):
            body = raw[1:]
            if continuing:
                pending.append(body)
            else:
                _flush()
                pending.append(body)
            continuing = body.rstrip().endswith("\\")
            continue

        # Non-recipe line: close any open command, then look for a new target.
        _flush()
        continuing = False
        if not raw.strip() or raw.startswith("#"):
            continue
        header = _TARGET_HEADER_RE.match(raw)
        if header and ":" in raw and not re.match(r"^[A-Za-z0-9_./%-]+\s*[:?+]?=", raw):
            current = header.group("name")

    _flush()
    return recipes


def _command_body(recipe_line: str) -> str:
    """Strip recipe-prefix chars (@ - +) and leading whitespace from a command."""

    return _RECIPE_PREFIX_RE.sub("", recipe_line.lstrip()).lstrip()


def _iter_helm_mutations(recipes: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Yield ``(target, logical_command)`` for every real helm upgrade/install."""

    found: list[tuple[str, str]] = []
    for target, commands in recipes.items():
        for command in commands:
            if _HELM_MUTATION_RE.match(_command_body(command)):
                found.append((target, command))
    return found


def test_makefile_parses_and_has_deploy_targets() -> None:
    """Sanity: the parser finds the three deploy targets we assert on."""

    recipes = _parse_recipes(MAKEFILE_PATH.read_text())
    for target in ("k8s-rolling-image", "k8s-upgrade", "k8s-install"):
        assert target in recipes, f"deploy target {target!r} not parsed from Makefile"


def test_rolling_image_delegates_to_runner_not_helm() -> None:
    """Invariant 1: the hot path delegates to the runner with --registry local."""

    recipes = _parse_recipes(MAKEFILE_PATH.read_text())
    commands = recipes["k8s-rolling-image"]
    joined = "\n".join(commands)

    # It must NOT run a raw helm mutation itself.
    assert not any(_HELM_MUTATION_RE.match(_command_body(cmd)) for cmd in commands), (
        f"k8s-rolling-image must delegate to the runner, not run helm directly:\n{joined}"
    )

    # It MUST delegate to the CD runner.
    assert "scripts.deploy.runner" in joined, (
        "k8s-rolling-image must invoke scripts.deploy.runner (the converged "
        f"preflight + repo-pin + mesh-gate path):\n{joined}"
    )

    # --registry local is mandatory: the runner's --registry default is 'hub',
    # which would deploy the Docker Hub image and re-open #394.
    assert "--registry local" in joined, (
        "k8s-rolling-image must pass --registry local (the runner defaults to "
        f"--registry hub, which re-opens the #394 footgun):\n{joined}"
    )


def test_every_helm_mutation_pins_internal_repository() -> None:
    """Invariant 2: every sanctioned helm upgrade/install pins the repo (#394)."""

    recipes = _parse_recipes(MAKEFILE_PATH.read_text())
    mutations = _iter_helm_mutations(recipes)

    # Guard-the-guard: there must be at least one real helm mutation to check,
    # otherwise a Makefile refactor could vacuously pass this test.
    assert mutations, (
        "no helm upgrade/install command found in the Makefile — the drift "
        "guard would be vacuous; update the parser if the deploy shape changed"
    )

    offenders = [
        (target, command)
        for target, command in mutations
        if "memoryServer.image.repository=" not in command
    ]
    assert not offenders, (
        "these helm upgrade/install commands do not pin "
        "memoryServer.image.repository= (#394 footgun re-opened):\n"
        + "\n".join(f"  [{target}] {command}" for target, command in offenders)
    )
