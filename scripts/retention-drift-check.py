#!/usr/bin/env python3
"""Retention drift-check (SPEC 2026-08-22-SPEC-retention-per-signal-windows).

Prints the EFFECTIVE retention window for every signal in the ratified
table and exits non-zero if any signal diverges. The ratified table itself
lives in ``charts/audittrace/files/retention-windows.yaml`` (the SSOT,
mirroring the SPEC #441 dashboards pattern) — this script never hardcodes
a second copy of the numbers, so there is exactly one place retention
windows can drift from the operator's ratified decision.

Two independent checks compose the "effective window" per signal:

1. **Live obs-stack parse** (Tempo ``block_retention``, Loki
   ``retention_period``, Prometheus's ``--storage.tsdb.retention.time``
   compose flag). Requires the sibling obs-stack repo
   (``$OBS_STACK_DIR``, default ``~/work/observability-stack``) to be
   checked out. This is the "is the LIVE deployed config actually what
   was ratified" half of the guard. Skipped, with a clear notice, when
   the obs-stack directory is absent — mirrors ``make check-dashboard-drift``
   (SPEC #441) so the pytest suite / CI stay green with no obs-stack
   present (CI-SAFE).
2. **Declared-signal echo** (MinIO/ChromaDB/Postgres — "no-expiry",
   ``source.kind: declared``). These have no externally observable live
   config reachable from this repo (the MinIO/S3 bucket-lifecycle apply is
   operator-attended, out of this WU's scope per the SPEC) — the script
   simply echoes the SSOT's declared window for the audit trail. Always
   reported, never gated on obs-stack presence.

Exit codes: ``0`` clean (or nothing checkable), ``1`` on any live
divergence.

Usage:
    python scripts/retention-drift-check.py
    python scripts/retention-drift-check.py --obs-stack-dir /path/to/observability-stack
    python scripts/retention-drift-check.py --config charts/audittrace/files/retention-windows.yaml
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = (
    REPO_ROOT / "charts" / "audittrace" / "files" / "retention-windows.yaml"
)
DEFAULT_OBS_STACK_DIR = (
    Path(os.environ.get("HOME", "")) / "work" / "observability-stack"
)

# Duration-string suffix -> hours. Go-duration-compatible subset (Tempo/Loki
# both accept "24h"/"720h"; the SSOT uses the day/week-friendly "7d"/"30d"
# form for readability — both normalise to hours for comparison).
_DURATION_UNIT_HOURS: dict[str, float] = {
    "h": 1.0,
    "d": 24.0,
    "w": 24.0 * 7,
}

# Sentinel windows that carry no comparable duration (never drift-checked
# against a live parsed value — only ever echoed from the SSOT).
_NO_EXPIRY = "no-expiry"


class RetentionDriftError(Exception):
    """Raised when the SSOT config is malformed — a config bug, not a
    live-vs-ratified divergence (those are reported, not raised)."""


def parse_duration_hours(value: str) -> float:
    """Parse a Go-style duration string (``"7d"``, ``"720h"``, ``"4w"``)
    into hours.

    Raises :class:`RetentionDriftError` on an unparseable string — a
    malformed duration in either the SSOT or a live obs-stack config is a
    configuration bug that must be surfaced loudly, not silently treated
    as "no drift".
    """
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([hdw])", value.strip())
    if not match:
        raise RetentionDriftError(
            f"unparseable duration {value!r} — expected a number followed "
            f"by one of {sorted(_DURATION_UNIT_HOURS)!r} (e.g. '7d', '24h')"
        )
    magnitude, unit = match.groups()
    return float(magnitude) * _DURATION_UNIT_HOURS[unit]


def load_ratified_config(config_path: Path) -> dict[str, Any]:
    """Load and minimally validate the SSOT retention-windows YAML."""
    if not config_path.exists():
        raise RetentionDriftError(f"retention config not found: {config_path}")
    data = yaml.safe_load(config_path.read_text())
    if not isinstance(data, dict) or "signals" not in data:
        raise RetentionDriftError(
            f"{config_path}: expected a top-level 'signals' mapping"
        )
    return data


def _get_pointer(data: Any, pointer: list[str]) -> Any:
    """Walk a nested-key pointer through a parsed YAML mapping. Returns
    ``None`` (never raises) when any hop is absent — a missing key means
    "not configured", which is itself a legitimate (and often DRIFT-worthy)
    effective value, not a script bug."""
    cursor = data
    for key in pointer:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor


def read_obs_stack_yaml_value(
    obs_stack_dir: Path, path: str, pointer: list[str]
) -> str | None:
    """Read a nested key out of an obs-stack YAML config file. Returns
    ``None`` when the file or the pointer path is absent (both are
    "not configured" — reported to the caller as a live value of ``None``,
    which will legitimately diverge from any ratified window)."""
    target = obs_stack_dir / path
    if not target.exists():
        return None
    parsed = yaml.safe_load(target.read_text())
    value = _get_pointer(parsed, pointer)
    return str(value) if value is not None else None


# Matches a long-form (``--flag=value``) or split-form (``--flag``, next
# token is the value) CLI flag inside a docker-compose ``command:`` list —
# handles both YAML flow-sequence and block-sequence rendering, since
# compose files in the wild use either.
def read_compose_flag_value(compose_path: Path, flag: str) -> str | None:
    """Extract ``--flag=value`` from a docker-compose file's ``command:``
    entries (any service). Returns ``None`` when the file or the flag is
    absent."""
    if not compose_path.exists():
        return None
    parsed = yaml.safe_load(compose_path.read_text())
    services = (parsed or {}).get("services", {}) if isinstance(parsed, dict) else {}
    prefix = f"{flag}="
    for service in services.values():
        if not isinstance(service, dict):
            continue
        for arg in service.get("command", []) or []:
            arg_str = str(arg)
            if arg_str.startswith(prefix):
                return arg_str[len(prefix) :]
    return None


class SignalResult:
    """The outcome of checking one signal: its declared (ratified) window,
    the live effective value (``None`` when not checkable), and whether
    that constitutes drift."""

    def __init__(
        self,
        name: str,
        ratified_window: str,
        effective: str | None,
        checked_live: bool,
    ) -> None:
        self.name = name
        self.ratified_window = ratified_window
        self.effective = effective
        self.checked_live = checked_live

    @property
    def drifted(self) -> bool:
        if not self.checked_live:
            return False
        if self.ratified_window == _NO_EXPIRY:
            # "declared" signals are never live-checked (see checked_live),
            # but guard defensively: no-expiry has no comparable duration.
            return False
        if self.effective is None:
            return True
        return parse_duration_hours(self.effective) != parse_duration_hours(
            self.ratified_window
        )

    def render(self) -> str:
        status = "DRIFT" if self.drifted else "OK"
        effective_display = (
            self.effective if self.effective is not None else "NOT CONFIGURED"
        )
        if not self.checked_live:
            effective_display = (
                f"{self.ratified_window} (declared; no live check target)"
            )
        return (
            f"  {self.name:<24} effective={effective_display!s:<40} "
            f"ratified={self.ratified_window:<10} [{status}]"
        )


def check_signal(
    name: str, spec: dict[str, Any], obs_stack_dir: Path | None
) -> SignalResult:
    ratified_window = spec["window"]
    source = spec.get("source", {})
    kind = source.get("kind")

    if kind == "declared" or obs_stack_dir is None:
        return SignalResult(name, ratified_window, None, checked_live=False)

    if kind == "obs_stack_yaml":
        effective = read_obs_stack_yaml_value(
            obs_stack_dir, source["path"], source["pointer"]
        )
        return SignalResult(name, ratified_window, effective, checked_live=True)

    if kind == "obs_stack_compose_flag":
        effective = read_compose_flag_value(
            obs_stack_dir / source["path"], source["flag"]
        )
        return SignalResult(name, ratified_window, effective, checked_live=True)

    raise RetentionDriftError(f"{name}: unknown source.kind {kind!r}")


def run_drift_check(config_path: Path, obs_stack_dir: Path) -> int:
    """Run the full drift-check and print a report. Returns the process
    exit code (0 clean, 1 on any live divergence)."""
    config = load_ratified_config(config_path)
    signals: dict[str, Any] = config["signals"]

    obs_stack_present = obs_stack_dir.is_dir()
    if not obs_stack_present:
        print(
            f"obs-stack not present, skipping live retention drift check "
            f"({obs_stack_dir})"
        )

    print(f"retention drift check — SSOT: {config_path}")
    results = [
        check_signal(name, spec, obs_stack_dir if obs_stack_present else None)
        for name, spec in signals.items()
    ]
    for result in results:
        print(result.render())

    drifted = [r for r in results if r.drifted]
    if drifted:
        print(
            f"retention drift: {len(drifted)} signal(s) diverge from the "
            "ratified table — see DRIFT rows above."
        )
        return 1

    print("no retention drift: all live-checkable signals match the ratified table.")
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="path to the SSOT retention-windows YAML (default: %(default)s)",
    )
    parser.add_argument(
        "--obs-stack-dir",
        type=Path,
        default=Path(os.environ.get("OBS_STACK_DIR", str(DEFAULT_OBS_STACK_DIR))),
        help=(
            "path to the sibling obs-stack checkout (default: $OBS_STACK_DIR "
            "or %(default)s)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)
    try:
        return run_drift_check(args.config, args.obs_stack_dir)
    except RetentionDriftError as exc:
        logger.error("retention drift check config error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
