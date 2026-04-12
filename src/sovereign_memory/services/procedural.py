"""Procedural memory service — Layer 2 of the 4-layer memory architecture (ADR-018).

Loads SKILL-*.md files from the filesystem and provides query-driven retrieval
based on keyword matching against skill names and content.

DESIGN §15 Phase 2: every method takes ``user_context: UserContext`` as the
first positional argument. SKILL files are filesystem-backed and shared, so
the parameter is pure plumbing here.
"""

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from langchain_core.documents import Document

from sovereign_memory.identity import UserContext
from sovereign_memory.logging_config import log_call

logger = logging.getLogger(__name__)


class ProceduralService(ABC):
    """Abstract procedural memory service — skill-based knowledge."""

    @abstractmethod
    def load(self, user_context: UserContext) -> list[Document]:
        """Load all SKILL documents."""

    @abstractmethod
    def search(self, user_context: UserContext, query: str) -> list[Document]:
        """Search skills by query relevance. No arbitrary caps."""

    @abstractmethod
    def as_context(self, user_context: UserContext, query: str) -> str:
        """Return matched skills formatted as context string."""


class FileProceduralService(ProceduralService):
    """Filesystem-based procedural service reading SKILL-*.md files."""

    def __init__(self, skill_dir: Path | str):
        self._skill_dir = Path(skill_dir)

    @log_call(logger=logger)
    def load(self, user_context: UserContext) -> list[Document]:
        """Load all SKILL-*.md files as LangChain Documents."""
        del user_context  # plumbing only — skills are shared, not per-user
        docs: list[Document] = []
        if not self._skill_dir.exists():
            return docs
        for f in sorted(self._skill_dir.glob("SKILL-*.md")):
            content = f.read_text(encoding="utf-8")
            skill_name = f.stem.replace("SKILL-", "")
            docs.append(
                Document(
                    page_content=content,
                    metadata={
                        "source": "procedural",
                        "file": f.name,
                        "skill": skill_name,
                    },
                )
            )
        return docs

    @log_call(logger=logger)
    def search(self, user_context: UserContext, query: str) -> list[Document]:
        """Filter skills by keyword relevance. No arbitrary caps on results.

        Searches the full skill content (not just the first 200 chars) so that
        descriptive frontmatter further down the file can still match a query.
        """
        skills = self.load(user_context)
        query_lower = query.lower()
        keywords = [kw for kw in query_lower.split() if len(kw) > 3]
        if not keywords:
            return []
        return [
            s
            for s in skills
            if any(
                kw in s.metadata.get("skill", "").lower()
                or kw in s.page_content.lower()
                for kw in keywords
            )
        ]

    @log_call(logger=logger)
    def as_context(self, user_context: UserContext, query: str) -> str:
        """Return matched skills formatted as a context section."""
        matched = self.search(user_context, query)
        if not matched:
            return ""
        lines = ["## Relevant Skills"]
        for s in matched:
            lines.append(f"- **{s.metadata['skill']}** ({s.metadata['file']})")
        return "\n".join(lines)


class MockProceduralService(ProceduralService):
    """Mock procedural service for unit testing."""

    def __init__(self):
        self._documents: list[Document] = []

    @log_call(logger=logger)
    def add_document(
        self, content: str, skill: str = "MockSkill", file: str = "SKILL-mock.md"
    ):
        """Add a document for testing."""
        self._documents.append(
            Document(
                page_content=content,
                metadata={"source": "procedural", "file": file, "skill": skill},
            )
        )

    @log_call(logger=logger)
    def load(self, user_context: UserContext) -> list[Document]:
        del user_context  # plumbing only
        return list(self._documents)

    @log_call(logger=logger)
    def search(self, user_context: UserContext, query: str) -> list[Document]:
        del user_context  # plumbing only
        query_lower = query.lower()
        keywords = [kw for kw in query_lower.split() if len(kw) > 3]
        if not keywords:
            return []
        return [
            d
            for d in self._documents
            if any(
                kw in d.metadata.get("skill", "").lower()
                or kw in d.page_content.lower()
                for kw in keywords
            )
        ]

    @log_call(logger=logger)
    def as_context(self, user_context: UserContext, query: str) -> str:
        matched = self.search(user_context, query)
        if not matched:
            return ""
        lines = ["## Relevant Skills"]
        for s in matched:
            lines.append(f"- **{s.metadata['skill']}** ({s.metadata['file']})")
        return "\n".join(lines)

    def reset(self):
        """Clear all documents."""
        self._documents.clear()
