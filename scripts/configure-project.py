#!/usr/bin/env python3
"""Set the X-Project header on the OpenCode config for the current project.

Implements the client-side half of ADR-029: every `/v1/chat/completions`
request carries an `X-Project` header so the memory-server can tag
`interactions`, `tool_calls`, and `sessions` rows with a real project
name (instead of the `"default"` fallback).

Usage:
    scripts/configure-project.py <project-name>
    scripts/configure-project.py AuditTrace-AI --launch
    scripts/configure-project.py Foo --config /custom/path/config.json
    scripts/configure-project.py AuditTrace-AI --dry-run
    scripts/configure-project.py --show

The script reads the OpenCode config (default
`~/.config/opencode/config.json`), merges
`options.headers["X-Project"]` into every provider entry, writes a
timestamped backup of the old file alongside, then writes the new
config atomically. With `--launch` it execs `opencode` afterwards.

Stdlib-only so it runs anywhere with a Python 3.11+ interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path.home() / ".config" / "opencode" / "config.json"
HEADER_NAME = "X-Project"


def _load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        sys.exit(f"error: OpenCode config not found at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {path} is not valid JSON: {exc}")


def _apply_project(config: dict[str, Any], project: str) -> tuple[dict[str, Any], int]:
    """Set options.headers[HEADER_NAME] = project on every provider entry.

    Returns (new_config, n_providers_updated). The config dict is modified
    in place and also returned for convenience.
    """
    providers = config.get("provider")
    if not isinstance(providers, dict) or not providers:
        sys.exit(
            'error: no "provider" object in config — cannot locate where to '
            "inject the X-Project header"
        )

    updated = 0
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        options = provider.setdefault("options", {})
        if not isinstance(options, dict):
            sys.exit(
                f"error: provider '{name}' has a non-dict options field — refusing "
                "to overwrite"
            )
        headers = options.setdefault("headers", {})
        if not isinstance(headers, dict):
            sys.exit(
                f"error: provider '{name}' has a non-dict options.headers field — "
                "refusing to overwrite"
            )
        headers[HEADER_NAME] = project
        updated += 1
    return config, updated


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, dest)
    return dest


def _atomic_write(path: Path, config: dict[str, Any]) -> None:
    body = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def _show(config: dict[str, Any]) -> None:
    providers = config.get("provider") or {}
    if not providers:
        print("(no providers configured)")
        return
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        options = provider.get("options") or {}
        headers = options.get("headers") or {}
        current = headers.get(HEADER_NAME, "(unset)")
        print(f"{name}: {HEADER_NAME} = {current}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set the X-Project header on the OpenCode config (ADR-029)."
    )
    parser.add_argument(
        "project",
        nargs="?",
        help="Project tag to set on every provider's X-Project header.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to OpenCode config.json (default: {DEFAULT_CONFIG}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the would-be config to stdout without writing.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print the current X-Project value per provider and exit.",
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Exec 'opencode' after a successful write.",
    )
    args = parser.parse_args()

    config = _load_config(args.config)

    if args.show:
        _show(config)
        return

    if not args.project:
        parser.error("a project name is required unless --show is used")

    new_config, updated = _apply_project(config, args.project)
    print(
        f"[configure-project] {HEADER_NAME} = {args.project!r} "
        f"set on {updated} provider(s)"
    )

    if args.dry_run:
        print("--- dry run — config not written ---")
        print(json.dumps(new_config, indent=2, ensure_ascii=False))
        return

    backup = _backup(args.config)
    _atomic_write(args.config, new_config)
    print(f"[configure-project] wrote {args.config} (backup: {backup.name})")

    if args.launch:
        exe = shutil.which("opencode")
        if not exe:
            sys.exit("error: --launch set but 'opencode' is not on PATH")
        print(f"[configure-project] exec {exe}")
        os.execv(exe, [exe])


if __name__ == "__main__":
    main()
