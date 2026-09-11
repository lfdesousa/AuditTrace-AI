"""Chart-values reading, deep-merge, and first-party image `--set`/digest helpers.

These are the SSOT the P2 chart-apply argv (:mod:`scripts.deploy.runner.helm`)
and the convergence check (:mod:`scripts.deploy.runner.convergence`) both read
the first-party console image pins from — see :func:`_read_chart_values`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from scripts.deploy import registry
from scripts.deploy.runner.config import (
    _CONSOLE_IMAGE_COMPONENTS,
    CHART_VALUES_FILE,
    DeployConfig,
)


def apply_image_tag(cfg: DeployConfig, image_ref: registry.ImageRef) -> str:
    """The value for ``memoryServer.image.tag`` at P2.

    When the digest is resolved (hub) we deploy by the IMMUTABLE digest using the
    canonical ``tag@digest`` form. The chart renders ``repository:tag`` and this
    repo's chart has no ``image.digest`` key, so ``<version>@sha256:...`` renders
    to ``repo:<version>@sha256:...`` — a valid reference where the digest pins
    the pull and the tag stays human-readable. For a local registry whose digest
    was soft-unresolved we keep the plain tag (documented).
    """
    if image_ref.digest:
        return f"{cfg.image_tag}@{image_ref.digest}"
    return cfg.image_tag


def _parse_values_file(path: Path) -> dict[str, Any]:
    """Parse a single chart values file to a mapping.

    An unreadable or malformed file degrades to ``{}`` rather than raising —
    a missing/broken values file is a chart-lint failure the preflight gate
    already catches, not something this helper should crash the apply on.
    """
    try:
        raw = path.read_text()
    except OSError:
        return {}
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base``; ``overlay`` wins.

    Nested mappings merge key-by-key (mirrors Helm's own ``-f`` merge
    semantics: a later values file overrides only the keys it sets, not
    sibling keys in the same block); any non-mapping value in ``overlay``
    (scalar, list, ``None``) replaces the base value outright. Neither input
    is mutated.
    """
    merged = dict(base)
    for key, value in overlay.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(base_value, value)
        else:
            merged[key] = value
    return merged


def _read_chart_values(
    path: Path = CHART_VALUES_FILE,
    *,
    values_files: Sequence[Path] = (),
) -> dict[str, Any]:
    """The EFFECTIVE chart values: base ``values.yaml`` deep-merged with each
    ``--values``/``-f`` overlay file, in order, later files winning (spec
    2026-09-10-SPEC-deploy-runner-target-overlay-aware).

    This is the SSOT :func:`console_image_set_args`, :func:`console_image_digests`
    and :meth:`DeployRunner._is_converged` read the first-party console image
    pins from. Before this fix the runner read ONLY base ``values.yaml``
    (``console.enabled: false`` there), so on the laptop target — where the
    console is enabled via the ``values-laptop.yaml`` overlay passed to
    ``helm upgrade`` but never read back by the runner — those three
    functions were permanently blind to the console, silently no-opping the
    apply-side console pin and the console-digest convergence check (origin
    finding ``finding-deploy-runner-not-overlay-aware-console-inert-20260910.md``).

    Read fresh off disk on every call: no live registry lookup, no
    ``docker inspect`` — deterministic and reproducible offline, unlike
    memory-server's tag->digest resolve (the chart's committed digests are
    already operator-verified against the registry, per the console-pins
    spec's §3).

    With no ``values_files`` (the default), behaviour is byte-identical to
    before this fix: a single parse of ``path``. Falsifiable: ignore
    ``values_files`` here (base-only, the pre-fix behaviour) and the
    overlay-aware neuter-proof test goes RED — the effective view stays
    ``console.enabled=false`` even when a laptop overlay enabling it is
    passed, exactly the live defect this spec closes.
    """
    merged = _parse_values_file(path)
    for overlay_path in values_files:
        merged = _deep_merge(merged, _parse_values_file(overlay_path))
    return merged


def console_image_set_args(chart_values: dict[str, Any]) -> list[str]:
    """The explicit ``--set`` argv asserting the first-party console images.

    Reads ``console.<component>.image.{repository,tag,digest}`` off the
    given (already-parsed) chart values — normally the committed
    ``values.yaml`` via :func:`_read_chart_values` — and renders them
    exactly as the chart's own templates expect: separate ``repository`` /
    ``tag`` / ``digest`` keys (``templates/console/deployment-librechat.yaml``
    and ``deployment-bff.yaml`` both render
    ``repository:tag{{ if digest }}@{{ digest }}{{ end }}``), UNLIKE the
    memory-server pin which folds the digest into the tag because that
    chart has no ``image.digest`` key at all.

    Passed as explicit ``--set`` args on every ``helm upgrade``, so they
    ALWAYS outrank any stale stored per-image override that
    ``--reset-then-reuse-values`` would otherwise re-apply forever — the
    #394-class defect this closes for console images (origin finding
    ``finding-deploy-runner-stale-setoverride-shadows-chart-20260910.md``,
    observed live at the v1.26.0 WU-6 Part C deploy).

    Returns ``[]`` when ``console.enabled`` is falsy in ``chart_values`` —
    the console isn't even templated in that case
    (``{{- if .Values.console.enabled }}``), so emitting a ``--set`` for it
    would be spurious. Also returns ``[]`` per-component when that
    component's ``image`` block isn't a mapping (defensive: a malformed
    values.yaml degrades to "no console pins asserted", never a crash), and
    skips any of ``repository``/``tag``/``digest`` that is empty/absent so a
    partially-specified block never emits a broken ``--set``.

    Falsifiable: drop this function's output from :func:`_helm_apply_cmd`
    (the pre-fix behaviour) and a stale stored console-image override once
    again silently shadows the chart default — proven by the neuter-proof
    test in ``tests/test_deploy_runner_console_image_pins.py``.
    """
    console = chart_values.get("console")
    if not isinstance(console, dict) or not console.get("enabled"):
        return []
    args: list[str] = []
    for component in _CONSOLE_IMAGE_COMPONENTS:
        block = console.get(component)
        image = block.get("image") if isinstance(block, dict) else None
        if not isinstance(image, dict):
            continue
        repository = image.get("repository")
        tag = image.get("tag")
        digest = image.get("digest")
        if repository:
            args += ["--set", f"console.{component}.image.repository={repository}"]
        if tag:
            args += ["--set", f"console.{component}.image.tag={tag}"]
        if digest:
            args += ["--set", f"console.{component}.image.digest={digest}"]
    return args


def console_image_digests(chart_values: dict[str, Any]) -> dict[str, str]:
    """The chart-pinned digest for each ENABLED console component (spec
    2026-09-10-SPEC-deploy-runner-convergence-first-party-image-complete).

    Reads the identical source :func:`console_image_set_args` parses
    (``console.<component>.image.digest`` off the given, already-parsed
    chart values — normally the committed ``values.yaml`` via
    :func:`_read_chart_values`) — never re-resolved from the registry, so
    convergence and the apply-side `--set` args can never disagree about
    what "the intended console digest" is.

    Returns ``{}`` when ``console.enabled`` is falsy (the console isn't
    even templated, so there is nothing to converge on) or the top-level
    ``console`` block isn't a mapping. A component is OMITTED (not
    reported as a mismatch candidate) when its ``image`` block isn't a
    mapping or its ``digest`` is empty/absent — there is nothing pinned to
    compare a live pod against in that case, mirroring
    :func:`console_image_set_args`'s own per-field skip.
    """
    console = chart_values.get("console")
    if not isinstance(console, dict) or not console.get("enabled"):
        return {}
    digests: dict[str, str] = {}
    for component in _CONSOLE_IMAGE_COMPONENTS:
        block = console.get(component)
        image = block.get("image") if isinstance(block, dict) else None
        digest = image.get("digest") if isinstance(image, dict) else None
        if digest:
            digests[component] = digest
    return digests


def _values_file_args(values_files: Sequence[Path]) -> list[str]:
    """``-f <path>`` argv pairs, one per overlay, in order."""
    args: list[str] = []
    for path in values_files:
        args += ["-f", str(path)]
    return args
