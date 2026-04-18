#!/usr/bin/env python3
"""Remove pre-fix Langfuse noise traces — the ones with no userId set.

Context: before commit e7005e0 (2026-04-18 morning), the Langfuse OTel
auto-instrumentor emitted traces with no userId, no input, no output,
and no meaningful name. That accumulated ~21 000 traces over the
lifetime of the instance. Commit 85872db tightened the export filter
so post-fix ingestion produces almost no additional noise, but the
historical backlog remains.

This script identifies those traces and (optionally) deletes them via
the Langfuse /api/public/traces/{id} DELETE endpoint. Default is
dry-run: print the count and a sample, then exit 0 without changing
anything. Pass --execute to actually delete.

Safety invariants
-----------------
1. **Never delete a trace that has userId set.** That's real audit
   data. The script filters on ``userId IS NULL`` (or empty string)
   server-side via the Langfuse query API.
2. **Never delete a trace that has meaningful content.** Second filter:
   both ``input`` and ``output`` must be falsy. If either is populated
   the trace might be a legitimate observation that happens to lack a
   userId (e.g., a health-check probe). Keep it.
3. **Dry-run is the default.** ``--execute`` must be passed explicitly.
4. **Rate-limit friendly.** One DELETE per 0.05 s (20 req/s) to avoid
   overwhelming the Langfuse web container.
5. **Resumable.** If interrupted, re-running restarts from the current
   API list; already-deleted traces simply won't appear.

Credentials
-----------
Public + secret keys are read from the running memory-server pod's
environment (the same ones used by the chat path). Fallback: env vars
LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY.

Usage
-----
    # Dry-run (default) — see what WOULD be deleted
    python scripts/langfuse-cleanup-noise-traces.py

    # Actually delete
    python scripts/langfuse-cleanup-noise-traces.py --execute

    # Cap the number of deletions per run (useful for incremental cleanup)
    python scripts/langfuse-cleanup-noise-traces.py --execute --max 1000
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import Any

import httpx

LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://192.168.1.231:3000")


def _resolve_credentials() -> tuple[str, str]:
    """Pull Langfuse keys from the running memory-server pod, fallback env."""
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    if pk and sk:
        return pk, sk

    try:
        out = subprocess.run(
            [
                "kubectl",
                "-n",
                "audittrace",
                "get",
                "deploy",
                "audittrace-memory-server",
                "-o",
                "jsonpath={.spec.template.spec.containers[0].env}",
            ],
            capture_output=True,
            text=True,
            check=True,
            env={
                **os.environ,
                "KUBECONFIG": os.environ.get(
                    "KUBECONFIG", os.path.expanduser("~/.kube/config")
                ),
            },
        )
        import json

        env = json.loads(out.stdout)
        env_map = {e.get("name", ""): e.get("value", "") for e in env}
        pk = pk or env_map.get("AUDITTRACE_LANGFUSE_PUBLIC_KEY", "")
        sk = sk or env_map.get("AUDITTRACE_LANGFUSE_SECRET_KEY", "")
    except Exception as exc:  # pragma: no cover - local dev only
        print(f"[warn] could not read keys from k8s: {exc}", file=sys.stderr)

    if not (pk and sk):
        print(
            "[fatal] Langfuse keys not available. Set LANGFUSE_PUBLIC_KEY + "
            "LANGFUSE_SECRET_KEY, or ensure the memory-server pod carries "
            "AUDITTRACE_LANGFUSE_* env vars and kubectl works.",
            file=sys.stderr,
        )
        sys.exit(2)
    return pk, sk


def _page_traces(
    client: httpx.Client, page: int, limit: int = 100
) -> tuple[list[dict[str, Any]], int]:
    resp = client.get(
        f"{LANGFUSE_HOST}/api/public/traces",
        params={"page": page, "limit": limit, "orderBy": "timestamp.asc"},
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("data", []), int(body.get("meta", {}).get("totalItems", 0))


def _is_noise(trace: dict[str, Any]) -> bool:
    """Safety invariant 1 + 2 combined: no user, no content."""
    if trace.get("userId"):
        return False
    if trace.get("input") or trace.get("output"):
        return False
    return True


def _delete(client: httpx.Client, trace_id: str) -> bool:
    try:
        resp = client.delete(f"{LANGFUSE_HOST}/api/public/traces/{trace_id}")
        return resp.status_code in (200, 204, 202)
    except Exception as exc:
        print(f"  ! failed to delete {trace_id}: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete traces. Default is dry-run.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="Cap number of deletions per run (0 = no cap).",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=0.05,
        help="Seconds between DELETE calls (default 0.05s = 20 req/s).",
    )
    args = parser.parse_args()

    pk, sk = _resolve_credentials()
    print(f"[info] host={LANGFUSE_HOST}")
    print(f"[info] mode={'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"[info] max={args.max or 'unlimited'}")
    print()

    noise_seen = 0
    noise_deleted = 0
    kept_with_user = 0
    kept_with_content = 0

    with httpx.Client(auth=(pk, sk), timeout=30) as client:
        page = 1
        while True:
            batch, total = _page_traces(client, page)
            if not batch:
                break
            if page == 1:
                print(f"[info] total traces visible: {total}")
                print()

            for trace in batch:
                tid = trace.get("id", "?")
                if trace.get("userId"):
                    kept_with_user += 1
                    continue
                if trace.get("input") or trace.get("output"):
                    kept_with_content += 1
                    continue

                noise_seen += 1
                ts = (trace.get("timestamp") or "")[:19]
                name = trace.get("name") or "<empty>"
                if noise_seen <= 5:
                    print(f"  [sample] {tid[:24]}  {ts}  name={name}")

                if args.execute:
                    if _delete(client, tid):
                        noise_deleted += 1
                        time.sleep(args.rate)

                if args.max and noise_deleted >= args.max:
                    print(f"[info] --max={args.max} reached; stopping")
                    break

            if args.max and noise_deleted >= args.max:
                break
            page += 1

    print()
    print("[summary]")
    print(f"  traces kept (have userId):        {kept_with_user}")
    print(f"  traces kept (have input/output):  {kept_with_content}")
    print(f"  noise traces identified:          {noise_seen}")
    if args.execute:
        print(f"  noise traces deleted:             {noise_deleted}")
    else:
        print(f"  noise traces that WOULD delete:   {noise_seen} (pass --execute)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
