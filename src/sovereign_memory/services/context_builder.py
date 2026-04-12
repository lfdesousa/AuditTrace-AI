"""Context builder service — aggregates all 4 memory layers (ADR-018).

Assembles a structured context string from episodic (ADRs), procedural (skills),
conversational (sessions), and semantic (ChromaDB RAG) memory layers.
All layers are injected via constructor — DI at the LangChain layer.

DESIGN §15 Phase 2: every method takes ``user_context: UserContext`` as
the first positional argument and passes it down to all four layers.

ADR-025 §Decision.1: this module also exposes ``build_ambient_context``,
a minimal always-injected system message used when
``SOVEREIGN_MEMORY_MODE=tools``. It carries identity, project, date, and
an enumeration of the visible memory tools — everything else becomes a
tool the LLM calls on demand.
"""

import logging
from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from sovereign_memory.identity import UserContext
from sovereign_memory.logging_config import log_call
from sovereign_memory.services.conversational import ConversationalService
from sovereign_memory.services.episodic import EpisodicService
from sovereign_memory.services.procedural import ProceduralService
from sovereign_memory.services.semantic import SemanticService

logger = logging.getLogger(__name__)


class ContextBuilderService(ABC):
    """Abstract context builder — aggregates all 4 memory layers."""

    @abstractmethod
    def build_system_context(
        self,
        user_context: UserContext,
        project: str | None = None,
        query: str | None = None,
    ) -> str:
        """Build a structured context string from all memory layers."""

    @abstractmethod
    def build_system_context_with_stats(
        self,
        user_context: UserContext,
        project: str | None = None,
        query: str | None = None,
    ) -> tuple[str, dict[str, int]]:
        """Build context and return per-layer retrieval stats."""


class DefaultContextBuilder(ContextBuilderService):
    """Default context builder — aggregates all 4 injected layer services.

    No arbitrary caps on results. Query-driven retrieval — "hello how are you"
    retrieves nothing, "KV cache compression" retrieves ADR-009.
    """

    def __init__(
        self,
        episodic: EpisodicService,
        procedural: ProceduralService,
        conversational: ConversationalService,
        semantic: SemanticService,
    ):
        self._episodic = episodic
        self._procedural = procedural
        self._conversational = conversational
        self._semantic = semantic

    @log_call(logger=logger)
    def build_system_context(
        self,
        user_context: UserContext,
        project: str | None = None,
        query: str | None = None,
    ) -> str:
        """Build context string. Delegates to build_system_context_with_stats."""
        ctx, _ = self.build_system_context_with_stats(user_context, project, query)
        return ctx

    @log_call(logger=logger)
    def build_system_context_with_stats(
        self,
        user_context: UserContext,
        project: str | None = None,
        query: str | None = None,
    ) -> tuple[str, dict[str, int]]:
        """Build context string and return per-layer stats."""
        sections: list[str] = []
        layer_stats: dict[str, int] = {}

        # Identity — always present, minimal
        sections.append(
            "## Profil\n"
            "Tu travailles avec Luis Filipe de Sousa, Solutions Architect, "
            "spécialisé IAM/OAuth2, auteur d'un Authorization Server OAuth2 (100K users). "
            f"Projet courant : **{project or 'non spécifié'}**."
        )

        if not query:
            return "\n\n---\n\n".join(sections), layer_stats

        # Layer 1: Episodic — ADRs
        try:
            matched_adrs = self._episodic.search(user_context, query)
            if matched_adrs:
                adr_lines = ["## Architecture Decisions"]
                for adr in matched_adrs:
                    adr_lines.append(
                        f"\n### {adr.metadata.get('title', 'ADR')}\n"
                        f"{adr.page_content[:400]}"
                    )
                sections.append("\n".join(adr_lines))
            layer_stats["episodic"] = len(matched_adrs)
        except Exception as e:
            logger.warning("Episodic layer failed: %s", e)
            layer_stats["episodic"] = 0

        # Layer 2: Procedural — Skills
        try:
            matched_skills = self._procedural.search(user_context, query)
            if matched_skills:
                skill_lines = ["## Relevant Skills"]
                for s in matched_skills:
                    skill_lines.append(
                        f"- **{s.metadata.get('skill', 'Skill')}** "
                        f"({s.metadata.get('file', '')})"
                    )
                sections.append("\n".join(skill_lines))
            layer_stats["procedural"] = len(matched_skills)
        except Exception as e:
            logger.warning("Procedural layer failed: %s", e)
            layer_stats["procedural"] = 0

        # Layer 3: Conversational — Sessions
        try:
            ctx_str = self._conversational.as_context(user_context, project or "")
            if ctx_str:
                sections.append(ctx_str)
            sessions = self._conversational.load_sessions(
                user_context, project or "", n=3
            )
            layer_stats["conversational"] = len(sessions)
        except Exception as e:
            logger.warning("Conversational layer failed: %s", e)
            layer_stats["conversational"] = 0

        # Layer 4: Semantic — ChromaDB RAG
        try:
            rag_docs = self._semantic.search(user_context, query, k=4)
            if rag_docs:
                rag_lines = ["## Relevant Context (RAG)"]
                for d in rag_docs:
                    src = d.metadata.get("source", d.metadata.get("file", "?"))
                    if "/" in str(src):
                        src = str(src).rsplit("/", 1)[-1]
                    rag_lines.append(f"\n**{src}**\n{d.page_content[:400]}")
                sections.append("\n".join(rag_lines))
            layer_stats["semantic"] = len(rag_docs)
        except Exception as e:
            logger.warning("Semantic layer failed: %s", e)
            layer_stats["semantic"] = 0

        context = "\n\n---\n\n".join(sections)
        return context, layer_stats


# ─────────────────── ADR-025 — ambient context generator ───────────────────


# Hard budget for the ambient context (ADR-025 §Decision.1). Counted as
# whitespace-split words; real LLM tokens are shorter than English words
# so this is a safe over-estimate. If we ever hit the ceiling, the
# per-tool description snippet is what gets trimmed first — the profile
# line is load-bearing for tool selection.
_AMBIENT_BUDGET_WORDS = 200
_DESCRIPTION_SNIPPET_LIMIT = 120


def build_ambient_context(
    user_context: UserContext,
    project: str | None,
    tools_visible: list[dict[str, Any]],
) -> str:
    """Build the minimal always-injected system message for tools mode.

    Shape (roughly)::

        ## Profil
        Tu travailles avec <username> (role: admin|user).
        Projet courant : <project>. Date : <YYYY-MM-DD>.

        ## Mémoire disponible
        Tu peux appeler les outils suivants si le contexte l'exige :
        - recall_decisions(query): <description>
        - recall_skills(query): <description>
        - recall_recent_sessions(project, n): <description>
        - recall_semantic(query, k): <description>

    Honest about authority:

    - ``user_context.is_admin`` is mirrored into the profile line so
      the LLM can reason about which tools are worth calling. ADR-025
      §Decision.4 Q4 accepts this as a minor information-disclosure
      vector in exchange for tool-selection quality.
    - Non-admin users do NOT see the word "admin" in their profile;
      the flag reflects the caller's actual authority.

    The output fits within a ~200-word budget regardless of how many
    memory tools are visible — the test suite pins this.
    """
    today_iso = date.today().isoformat()
    role_label = "admin" if user_context.is_admin else "user"
    project_label = project or "unspecified"

    lines: list[str] = []
    lines.append("## Profil")
    lines.append(
        f"Tu travailles avec {user_context.username} (rôle: {role_label}). "
        f"Projet courant : {project_label}. Date : {today_iso}."
    )

    if tools_visible:
        lines.append("")
        lines.append("## Mémoire disponible")
        lines.append("Tu peux appeler les outils suivants si le contexte l'exige :")
        for t in tools_visible:
            fn = t.get("function") or {}
            name = fn.get("name", "?")
            desc = fn.get("description", "")
            # Trim aggressively so the 4-tool case stays inside the
            # _AMBIENT_BUDGET_WORDS budget even with verbose descriptions.
            snippet = " ".join(desc.split())[:_DESCRIPTION_SNIPPET_LIMIT]
            lines.append(f"- **{name}**: {snippet}")

    out = "\n".join(lines)
    # Fail loud in dev if a future edit blows the budget — better a
    # logged warning at build time than a hidden prompt-size regression.
    word_count = len(out.split())
    if word_count > _AMBIENT_BUDGET_WORDS:
        logger.warning(
            "ambient context exceeded budget: %d words (budget %d)",
            word_count,
            _AMBIENT_BUDGET_WORDS,
        )
    return out


class MockContextBuilder(ContextBuilderService):
    """Mock context builder for unit testing."""

    def __init__(self, static_context: str = ""):
        self._static_context = static_context

    @log_call(logger=logger)
    def build_system_context(
        self,
        user_context: UserContext,
        project: str | None = None,
        query: str | None = None,
    ) -> str:
        del user_context  # mock ignores identity
        return self._static_context

    @log_call(logger=logger)
    def build_system_context_with_stats(
        self,
        user_context: UserContext,
        project: str | None = None,
        query: str | None = None,
    ) -> tuple[str, dict[str, int]]:
        del user_context
        return self._static_context, {}
