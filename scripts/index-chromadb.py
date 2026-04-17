#!/usr/bin/env python3
"""Index documents into ChromaDB collections for semantic search (ADR-027).

Reads from source directories on the host filesystem, extracts text (including
PDFs via pymupdf), chunks, and pushes to ChromaDB on localhost:18000.

Usage:
    # Index all collections
    python scripts/index-chromadb.py --user-id kc-luis-001

    # Index specific collections only
    python scripts/index-chromadb.py --collections decisions skills

    # Preview without writing
    python scripts/index-chromadb.py --dry-run --user-id kc-luis-001

    # Verbose output
    python scripts/index-chromadb.py --user-id kc-luis-001 --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
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
    """Index documents into a ChromaDB collection.

    Each doc dict has: id, document, metadata.
    Returns the number of chunks indexed.
    """
    if dry_run:
        logger.info("  [DRY RUN] %s: %d chunks", name, len(docs))
        return len(docs)

    # Delete and recreate for idempotency
    try:
        client.delete_collection(name)
    except Exception:
        pass
    collection = client.get_or_create_collection(name=name)

    # Batch in groups of 100 (ChromaDB recommendation)
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
    # Check skills source directly if procedural dir is empty
    if not list(PROCEDURAL_DIR.glob("SKILL-*.md")):
        # Fall back to claude-config skills
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index documents into ChromaDB (ADR-027)"
    )
    parser.add_argument(
        "--collections",
        nargs="+",
        choices=["decisions", "skills", "ai_research", "scm_coursework"],
        default=None,
        help="Index specific collections only (default: all).",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="Keycloak user ID for private collection metadata. Required for ai_research/scm_coursework.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without writing."
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    target_collections = args.collections or [
        "decisions",
        "skills",
        "ai_research",
        "scm_coursework",
    ]

    # Require user_id for private collections
    private_collections = {"ai_research", "scm_coursework"}
    if private_collections & set(target_collections) and not args.user_id:
        parser.error(
            "--user-id is required for ai_research and scm_coursework collections"
        )

    # Read ChromaDB connection parameters
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

    # Build and index each collection
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


if __name__ == "__main__":
    main()
