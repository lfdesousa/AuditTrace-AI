#!/usr/bin/env python3
"""Index documents into ChromaDB via the AuditTrace memory server API.

Uploads files to ``POST /memory/upload`` and triggers reindexing via
``POST /memory/index``.  The memory server is the single gateway — this
script never talks to MinIO or ChromaDB directly.

Usage:
    # Upload ADRs as episodic memory
    python scripts/index-chromadb.py \\
      --server https://localhost:30952 -k \\
      --token "$TOKEN" \\
      --upload-dir docs/ --layer episodic

    # Upload skills as procedural memory
    python scripts/index-chromadb.py \\
      --server https://localhost:30952 -k \\
      --token "$TOKEN" \\
      --upload-dir ~/work/claude-config/skills/ --layer procedural

    # Trigger reindex only (no upload)
    python scripts/index-chromadb.py \\
      --server https://localhost:30952 -k \\
      --token "$TOKEN" \\
      --index-only

    # Legacy mode — direct ChromaDB indexing (backward compat)
    python scripts/index-chromadb.py --legacy --user-id kc-luis-001
    python scripts/index-chromadb.py --legacy --collections decisions skills
    python scripts/index-chromadb.py --legacy --dry-run --user-id kc-luis-001
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
PROCEDURAL_DIR = REPO_ROOT / "memory" / "procedural"
AI_KNOWLEDGE = Path.home() / "work" / "ai-knowledge"
SCM_KNOWLEDGE = Path.home() / "work" / "scm-knowledge"

# Chunking parameters — ChromaDB's default embedding model (all-MiniLM-L6-v2)
# has a 256 word-piece token limit. 1500 chars is a safe ceiling that avoids
# truncation while keeping chunks semantically coherent.
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200


# ── shared helpers (used by both legacy and HTTP modes) ──────────────────────


def _chunk_text(
    text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """Split text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start = end - overlap
    return chunks


def _extract_pdf_text(file_path: Path) -> list[tuple[str, int]]:
    """Extract text from PDF, returning (text, page_number) tuples."""
    try:
        import pymupdf

        doc = pymupdf.open(str(file_path))
        pages = []
        for i, page in enumerate(doc):
            text = page.get_text().strip()
            if text:
                pages.append((text, i + 1))
        doc.close()
        return pages
    except Exception as exc:
        logger.warning("PDF extraction failed for %s: %s", file_path.name, exc)
        return []


def _read_text_file(file_path: Path) -> str:
    """Read a text file, handling encoding gracefully."""
    for encoding in ("utf-8", "latin-1"):
        try:
            return file_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _doc_id(collection: str, source: str, chunk_idx: int) -> str:
    """Generate a deterministic document ID."""
    raw = f"{collection}:{source}:{chunk_idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── HTTP-client mode ─────────────────────────────────────────────────────────


def _resolve_token(args: argparse.Namespace) -> str:
    """Resolve the bearer token from CLI arg, env var, or login script."""
    if args.token:
        return args.token
    env_token = os.environ.get("AUDITTRACE_TOKEN")
    if env_token:
        return env_token
    logger.error(
        "No auth token. Pass --token, set AUDITTRACE_TOKEN, "
        "or run: TOKEN=$(scripts/audittrace-login --show)"
    )
    sys.exit(1)


def _upload_files(
    server: str,
    token: str,
    upload_dir: Path,
    layer: str,
    verify: bool,
) -> int:
    """Upload all .md files from *upload_dir* to POST /memory/upload."""
    import httpx

    uploaded = 0
    md_files = sorted(upload_dir.rglob("*.md"))
    if not md_files:
        logger.warning("No .md files found in %s", upload_dir)
        return 0

    headers = {"Authorization": f"Bearer {token}"}
    for f in md_files:
        relative = f.relative_to(upload_dir)
        filename = str(relative)
        logger.info("Uploading %s (%s) ...", filename, layer)
        try:
            with open(f, "rb") as fh:
                resp = httpx.post(
                    f"{server}/memory/upload",
                    params={"layer": layer, "filename": filename},
                    files={"file": (filename, fh, "text/markdown")},
                    headers=headers,
                    verify=verify,
                    timeout=30,
                )
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                "  -> %s (%d bytes)", data.get("key"), data.get("size_bytes", 0)
            )
            uploaded += 1
        except Exception as exc:
            logger.error("  FAILED: %s", exc)

    return uploaded


def _trigger_index(
    server: str,
    token: str,
    collections: str | None,
    verify: bool,
) -> None:
    """Call POST /memory/index to trigger reindexing."""
    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    params: dict[str, str] = {}
    if collections:
        params["collections"] = collections

    logger.info(
        "Triggering reindex%s ...",
        f" (collections={collections})" if collections else "",
    )
    resp = httpx.post(
        f"{server}/memory/index",
        params=params,
        headers=headers,
        verify=verify,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(
        "Indexing complete: %d total chunks across %d collections in %.1fs",
        data.get("total_chunks", 0),
        len(data.get("collections", {})),
        data.get("duration_s", 0),
    )
    for col, count in data.get("collections", {}).items():
        logger.info("  %s: %d chunks", col, count)


def run_http_mode(args: argparse.Namespace) -> None:
    """HTTP-client mode: upload files and/or trigger index via the API."""
    token = _resolve_token(args)
    verify = not args.insecure

    if not args.index_only:
        if not args.upload_dir:
            logger.error("--upload-dir is required (or use --index-only)")
            sys.exit(1)
        if not args.layer:
            logger.error("--layer is required when uploading")
            sys.exit(1)
        upload_dir = Path(args.upload_dir).expanduser().resolve()
        if not upload_dir.is_dir():
            logger.error("Upload directory does not exist: %s", upload_dir)
            sys.exit(1)
        uploaded = _upload_files(args.server, token, upload_dir, args.layer, verify)
        logger.info("Uploaded %d files", uploaded)

    # Always trigger reindex after upload (or if --index-only)
    _trigger_index(args.server, token, args.reindex_collections, verify)


# ── legacy direct-to-ChromaDB mode ──────────────────────────────────────────


def _get_chromadb_client(url: str, token: str | None):
    """Create ChromaDB HTTP client."""
    import chromadb
    from chromadb.config import Settings

    settings = (
        Settings(
            chroma_client_auth_provider="chromadb.auth.token_authn.TokenAuthClientProvider",
            chroma_client_auth_credentials=token or "",
        )
        if token
        else Settings()
    )
    return chromadb.HttpClient(
        host=url.split("://")[-1].split(":")[0],
        port=int(url.split(":")[-1]),
        settings=settings,
    )


def _index_collection(
    client,
    name: str,
    docs: list[dict],
    dry_run: bool = False,
) -> int:
    """Index documents into a ChromaDB collection."""
    if dry_run:
        logger.info("  [DRY RUN] %s: %d chunks", name, len(docs))
        return len(docs)

    try:
        client.delete_collection(name)
    except Exception:
        pass
    collection = client.get_or_create_collection(name=name)

    batch_size = 100
    for i in range(0, len(docs), batch_size):
        batch = docs[i : i + batch_size]
        collection.add(
            ids=[d["id"] for d in batch],
            documents=[d["document"] for d in batch],
            metadatas=[d["metadata"] for d in batch],
        )

    logger.info("  %s: %d chunks indexed", name, len(docs))
    return len(docs)


def build_decisions_docs() -> list[dict]:
    """Build document list from ADR-*.md files."""
    docs = []
    for f in sorted(DOCS_DIR.glob("ADR-*.md")):
        content = _read_text_file(f)
        if not content:
            continue
        chunks = _chunk_text(content)
        for i, chunk in enumerate(chunks):
            docs.append(
                {
                    "id": _doc_id("decisions", f.name, i),
                    "document": chunk,
                    "metadata": {
                        "source": f.name,
                        "category": "adr",
                        "file_type": "md",
                        "chunk": i,
                    },
                }
            )
    return docs


def build_skills_docs() -> list[dict]:
    """Build document list from SKILL-*.md files in memory/procedural/."""
    docs = []
    if not list(PROCEDURAL_DIR.glob("SKILL-*.md")):
        skills_src = Path.home() / "work" / "claude-config" / "skills"
        if skills_src.exists():
            for skill_dir in sorted(skills_src.iterdir()):
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    content = _read_text_file(skill_file)
                    if not content:
                        continue
                    domain = skill_dir.name
                    fname = f"SKILL-{domain}.md"
                    chunks = _chunk_text(content)
                    for i, chunk in enumerate(chunks):
                        docs.append(
                            {
                                "id": _doc_id("skills", fname, i),
                                "document": chunk,
                                "metadata": {
                                    "source": fname,
                                    "category": "skill",
                                    "file_type": "md",
                                    "skill": domain,
                                    "chunk": i,
                                },
                            }
                        )
            return docs

    for f in sorted(PROCEDURAL_DIR.glob("SKILL-*.md")):
        content = _read_text_file(f)
        if not content:
            continue
        skill_name = f.stem.replace("SKILL-", "")
        chunks = _chunk_text(content)
        for i, chunk in enumerate(chunks):
            docs.append(
                {
                    "id": _doc_id("skills", f.name, i),
                    "document": chunk,
                    "metadata": {
                        "source": f.name,
                        "category": "skill",
                        "file_type": "md",
                        "skill": skill_name,
                        "chunk": i,
                    },
                }
            )
    return docs


def _build_knowledge_docs(
    source_dir: Path,
    collection_name: str,
    user_id: str | None,
) -> list[dict]:
    """Build document list from a knowledge directory (text + PDF)."""
    docs = []
    if not source_dir.exists():
        logger.warning("Knowledge source not found: %s", source_dir)
        return docs

    indexable_suffixes = {".md", ".txt", ".py", ".json"}

    for f in sorted(source_dir.rglob("*")):
        if not f.is_file():
            continue
        suffix = f.suffix.lower()
        relative = f.relative_to(source_dir)
        category = str(relative.parent) if relative.parent != Path(".") else "root"

        base_metadata: dict = {
            "source": f.name,
            "category": category,
            "file_type": suffix.lstrip("."),
        }
        if user_id:
            base_metadata["user_id"] = user_id

        if suffix == ".pdf":
            pages = _extract_pdf_text(f)
            for page_text, page_num in pages:
                chunks = _chunk_text(page_text)
                for i, chunk in enumerate(chunks):
                    docs.append(
                        {
                            "id": _doc_id(
                                collection_name, f"{relative}:p{page_num}", i
                            ),
                            "document": chunk,
                            "metadata": {**base_metadata, "page": page_num, "chunk": i},
                        }
                    )
        elif suffix in indexable_suffixes:
            content = _read_text_file(f)
            if not content.strip():
                continue
            chunks = _chunk_text(content)
            for i, chunk in enumerate(chunks):
                docs.append(
                    {
                        "id": _doc_id(collection_name, str(relative), i),
                        "document": chunk,
                        "metadata": {**base_metadata, "chunk": i},
                    }
                )

    return docs


def run_legacy_mode(args: argparse.Namespace) -> None:
    """Legacy direct-to-ChromaDB mode (backward compat)."""
    target_collections = args.collections or [
        "decisions",
        "skills",
        "ai_research",
        "scm_coursework",
    ]

    private_collections = {"ai_research", "scm_coursework"}
    if private_collections & set(target_collections) and not args.user_id:
        logger.error(
            "--user-id is required for ai_research and scm_coursework collections"
        )
        sys.exit(1)

    chroma_url = os.environ.get("AUDITTRACE_CHROMA_URL", "http://localhost:18000")
    chroma_token = os.environ.get("AUDITTRACE_CHROMA_TOKEN", "")
    if not chroma_token:
        token_file = REPO_ROOT / "secrets" / "chroma_token.txt"
        if token_file.exists():
            chroma_token = token_file.read_text().strip()

    if not args.dry_run:
        client = _get_chromadb_client(chroma_url, chroma_token or None)
    else:
        client = None

    start = time.time()
    total_chunks = 0

    collection_builders: dict[str, tuple] = {
        "decisions": (build_decisions_docs, {}),
        "skills": (build_skills_docs, {}),
        "ai_research": (
            _build_knowledge_docs,
            {
                "source_dir": AI_KNOWLEDGE,
                "collection_name": "ai_research",
                "user_id": args.user_id,
            },
        ),
        "scm_coursework": (
            _build_knowledge_docs,
            {
                "source_dir": SCM_KNOWLEDGE,
                "collection_name": "scm_coursework",
                "user_id": args.user_id,
            },
        ),
    }

    for name in target_collections:
        builder_fn, kwargs = collection_builders[name]
        logger.info("Building %s...", name)
        docs = builder_fn(**kwargs) if kwargs else builder_fn()
        logger.info(
            "  %s: %d chunks from %d sources",
            name,
            len(docs),
            len({d["metadata"]["source"] for d in docs}),
        )
        if docs:
            total_chunks += _index_collection(client, name, docs, dry_run=args.dry_run)

    elapsed = time.time() - start
    logger.info(
        "Indexing complete: %d total chunks across %d collections in %.1fs",
        total_chunks,
        len(target_collections),
        elapsed,
    )


# ── CLI entrypoint ───────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index documents into ChromaDB via the AuditTrace memory server API"
    )

    # HTTP-client mode args
    parser.add_argument(
        "--server",
        default="https://localhost:30952",
        help="Memory server URL (default: https://localhost:30952).",
    )
    parser.add_argument(
        "--upload-dir",
        default=None,
        help="Upload all .md files from this directory.",
    )
    parser.add_argument(
        "--layer",
        choices=["episodic", "procedural"],
        default=None,
        help="Memory layer to upload to.",
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="Skip upload, just trigger reindex.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token for auth (or set AUDITTRACE_TOKEN env).",
    )
    parser.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="Skip TLS verification.",
    )
    parser.add_argument(
        "--reindex-collections",
        default=None,
        help="Comma-separated collections for reindex (default: all).",
    )

    # Legacy mode args (backward compat)
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Use legacy direct-to-ChromaDB indexing (bypass the API).",
    )
    parser.add_argument(
        "--collections",
        nargs="+",
        choices=["decisions", "skills", "ai_research", "scm_coursework"],
        default=None,
        help="(Legacy) Index specific collections only.",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="(Legacy) Keycloak user ID for private collections.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="(Legacy) Preview without writing.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.legacy:
        run_legacy_mode(args)
    else:
        run_http_mode(args)


if __name__ == "__main__":
    main()
