#!/usr/bin/env python3
"""Upload memory content to MinIO and trigger ChromaDB indexing (ADR-027).

Creates buckets (memory-shared, memory-private) and uploads:
  - ADRs from docs/ → memory-shared/episodic/
  - Skills from ~/work/claude-config/skills/ → memory-shared/procedural/
  - Private knowledge → memory-private/{user_id}/

Usage:
    # Full seed (MinIO + ChromaDB)
    python scripts/seed-memory.py --user-id kc-luis-001

    # MinIO only (skip ChromaDB indexing)
    python scripts/seed-memory.py --user-id kc-luis-001 --skip-index

    # Shared content only (no private knowledge)
    python scripts/seed-memory.py --shared-only
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# Resolve paths relative to repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
SKILLS_SRC = Path.home() / "work" / "claude-config" / "skills"
AI_KNOWLEDGE = Path.home() / "work" / "ai-knowledge"
SCM_KNOWLEDGE = Path.home() / "work" / "scm-knowledge"


def _get_minio_client(url: str, access_key: str, secret_key: str):
    """Create MinIO client from connection parameters."""
    from urllib.parse import urlparse

    from minio import Minio

    parsed = urlparse(url)
    endpoint = parsed.netloc or parsed.path
    secure = parsed.scheme == "https"
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)


def _ensure_bucket(client, bucket: str) -> None:
    """Create bucket if it doesn't exist."""
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        logger.info("Created bucket: %s", bucket)
    else:
        logger.info("Bucket exists: %s", bucket)


def _upload_file(client, bucket: str, object_name: str, file_path: Path) -> None:
    """Upload a single file to MinIO."""
    client.fput_object(bucket, object_name, str(file_path))
    logger.info("  %s → %s/%s", file_path.name, bucket, object_name)


def seed_shared_episodic(client, bucket: str) -> int:
    """Upload ADR-*.md files to memory-shared/episodic/."""
    count = 0
    for f in sorted(DOCS_DIR.glob("ADR-*.md")):
        _upload_file(client, bucket, f"episodic/{f.name}", f)
        count += 1
    logger.info("Uploaded %d ADRs to %s/episodic/", count, bucket)
    return count


def seed_shared_procedural(client, bucket: str) -> int:
    """Upload SKILL files from claude-config to memory-shared/procedural/."""
    count = 0
    if not SKILLS_SRC.exists():
        logger.warning("Skills source not found: %s", SKILLS_SRC)
        return 0
    for skill_dir in sorted(SKILLS_SRC.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            domain = skill_dir.name
            object_name = f"procedural/SKILL-{domain}.md"
            _upload_file(client, bucket, object_name, skill_file)
            count += 1
    logger.info("Uploaded %d skills to %s/procedural/", count, bucket)
    return count


def seed_private_knowledge(
    client, bucket: str, user_id: str, source_dir: Path, category: str
) -> int:
    """Upload knowledge files to memory-private/{user_id}/{category}/."""
    count = 0
    if not source_dir.exists():
        logger.warning("Knowledge source not found: %s", source_dir)
        return 0
    for f in sorted(source_dir.rglob("*")):
        if not f.is_file():
            continue
        # Skip non-indexable binary files (except PDFs)
        if f.suffix.lower() not in (".md", ".txt", ".py", ".pdf", ".json"):
            continue
        relative = f.relative_to(source_dir)
        object_name = f"{user_id}/{category}/{relative}"
        _upload_file(client, bucket, object_name, f)
        count += 1
    logger.info("Uploaded %d files to %s/%s/%s/", count, bucket, user_id, category)
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed MinIO memory buckets (ADR-027)")
    parser.add_argument(
        "--user-id",
        default=None,
        help="Keycloak user ID (sub claim) for private knowledge. Required unless --shared-only.",
    )
    parser.add_argument(
        "--shared-only",
        action="store_true",
        help="Only upload shared content (ADRs + skills), skip private knowledge.",
    )
    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="Skip ChromaDB indexing after upload.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.shared_only and not args.user_id:
        parser.error(
            "--user-id is required for private knowledge (or use --shared-only)"
        )

    # Read connection parameters from environment or .env file
    url = os.environ.get("AUDITTRACE_MINIO_URL", "http://localhost:19000")
    access_key = os.environ.get("AUDITTRACE_MINIO_ACCESS_KEY", "minioadmin")
    secret_key = os.environ.get("AUDITTRACE_MINIO_SECRET_KEY", "")

    if not secret_key:
        # Try reading from secrets file
        secrets_file = REPO_ROOT / "secrets" / "minio_secret_key.txt"
        if secrets_file.exists():
            secret_key = secrets_file.read_text().strip()
        else:
            logger.error(
                "AUDITTRACE_MINIO_SECRET_KEY not set and %s not found. "
                "Run scripts/setup-secrets.sh first.",
                secrets_file,
            )
            sys.exit(1)

    shared_bucket = os.environ.get("AUDITTRACE_MINIO_SHARED_BUCKET", "memory-shared")
    private_bucket = os.environ.get("AUDITTRACE_MINIO_PRIVATE_BUCKET", "memory-private")

    client = _get_minio_client(url, access_key, secret_key)

    # Create buckets
    _ensure_bucket(client, shared_bucket)
    if not args.shared_only:
        _ensure_bucket(client, private_bucket)

    # Upload shared content
    total = 0
    total += seed_shared_episodic(client, shared_bucket)
    total += seed_shared_procedural(client, shared_bucket)

    # Upload private knowledge
    if not args.shared_only and args.user_id:
        total += seed_private_knowledge(
            client, private_bucket, args.user_id, AI_KNOWLEDGE, "ai-research"
        )
        total += seed_private_knowledge(
            client, private_bucket, args.user_id, SCM_KNOWLEDGE, "scm-coursework"
        )

    logger.info("Seed complete: %d files uploaded", total)

    # Trigger ChromaDB indexing
    if not args.skip_index:
        logger.info("Running ChromaDB indexer...")
        index_script = REPO_ROOT / "scripts" / "index-chromadb.py"
        cmd = [sys.executable, str(index_script)]
        if args.user_id:
            cmd.extend(["--user-id", args.user_id])
        if args.verbose:
            cmd.append("--verbose")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            logger.error("ChromaDB indexing failed (exit code %d)", result.returncode)
            sys.exit(result.returncode)


if __name__ == "__main__":
    main()
