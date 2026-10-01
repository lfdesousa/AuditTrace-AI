"""llama.cpp decision client (#459 WU-459-1).

``decide(state, question_id)`` asks ONE closed question and returns a
:class:`DecisionResult`. Contract:

* **Never raises an ``Exception``.** Every failure maps to a ``result`` /
  ``error_code`` (fail-open to the caller). ``asyncio.CancelledError``,
  ``KeyboardInterrupt`` and ``SystemExit`` are ``BaseException`` and PROPAGATE.
* **Disabled means no network:** ``decision_url == ""`` returns
  ``unavailable``/``disabled`` before any call.
* **One total budget:** ``decision_timeout_ms`` is a single deadline across
  every call (``/props`` + special-token ``/tokenize`` + user ``/tokenize`` +
  ``/completion``).
* **Model identity is ATTESTED:** ``GET /props`` once per client; the server's
  alias must equal ``decision_model_alias`` or the result is
  ``model_identity_mismatch`` with NO inference call. The alias is a launch
  label, not a content hash: content attestation rests on the row's
  ``server_model_file`` + ``decision_model_digest_configured`` plus a
  deploy-time host hash of the file.
* **Template injection closed by construction:** the prompt is sent as TOKEN
  IDS. User content is tokenised in ONE ``parse_special:false`` call (piece by
  piece changes the ids) and the template's special ids are placed around it.
  Because ``parse_special:false`` still returns some control strings
  (``<think>`` etc.) as single ids on the pinned tokenizer, the user-content
  ids are also checked against a DENYLIST of control ids (resolved by
  tokenising each fixed control string with ``parse_special:true``). Control
  tokens that only the GGUF tokenizer metadata marks as control, and that are
  not in that fixed list, cannot be enumerated over HTTP; the live probe
  cross-checks the fixed list on the real server.
* **Secrets:** ``decision_api_key`` is sent as ``Authorization: Bearer`` and is
  never logged, recorded or placed in a repr.
* **One process-resident client per settings;** use ``async with`` (the app
  lifespan owns it in WU-459-2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Self

import httpx
from opentelemetry import trace
from opentelemetry.trace import Span, Tracer

from audittrace.config import Settings
from audittrace.services.decision import _llama_http as llama
from audittrace.services.decision import template as tpl
from audittrace.services.decision.distribution import (
    choice_and_confidence,
    entropy,
    renormalise,
)
from audittrace.services.decision.errors import DecisionError, result_for
from audittrace.services.decision.questions import Question, get_question
from audittrace.services.decision.result import DecisionResult

logger = logging.getLogger(__name__)

RUNTIME = "llama.cpp"
SPAN_NAME = "decision.decide"


class _TokenTables:
    """Per-client cache of the tokenizer facts every call needs."""

    def __init__(
        self,
        special_ids: dict[str, int],
        piece_ids: dict[str, list[int]],
        denylist: frozenset[int],
    ) -> None:
        self.special_ids = special_ids
        self.piece_ids = piece_ids
        self.denylist = denylist
        self.letter_ids: dict[str, int] = {}


def _carries_control_token(
    content: str, content_ids: list[int], denylist: frozenset[int]
) -> bool:
    """True when user content can reach a control token (fail closed).

    Two independent signals, because each alone is incomplete on the pinned
    tokenizer: ``parse_special:false`` splits ``<|im_start|>``, ``<|im_end|>``
    and ``<|endoftext|>`` into plain-text pieces (no control id appears, so
    only the literal-string check sees them) but still returns ``<think>``,
    ``</think>``, ``<tool_call>`` and ``</tool_call>`` as single control ids
    (caught by BOTH signals), and a text variant the tokenizer normalises to a
    control id is caught only by the id check.
    """
    if any(control in content for control in tpl.CONTROL_STRINGS):
        return True
    return not denylist.isdisjoint(content_ids)


class _Progress:
    """Facts learned so far; whatever is known lands on a failure row too."""

    def __init__(self, started_at: datetime) -> None:
        self.started_at = started_at
        self.identity: llama.ServerIdentity | None = None
        self.question: Question | None = None
        self.input_sha256: str | None = None
        self.allowed_ids: tuple[int, ...] | None = None
        self.request: dict[str, Any] | None = None
        self.raw: dict[int, float] | None = None
        self.dist: tuple[float, ...] | None = None


class LlamaCppDecisionClient:
    """Async decision client for one llama.cpp server."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._s = settings
        self._base = settings.decision_url.rstrip("/")
        self._timeout_s = settings.decision_timeout_ms / 1000.0
        headers = (
            {"Authorization": f"Bearer {settings.decision_api_key}"}
            if settings.decision_api_key
            else {}
        )
        self._http = httpx.AsyncClient(
            transport=transport, headers=headers, timeout=self._timeout_s
        )
        self._tracer = tracer or trace.get_tracer(__name__)
        self._identity: llama.ServerIdentity | None = None
        self._tables: _TokenTables | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        # Never include settings, headers or the key.
        return f"<LlamaCppDecisionClient url={self._base!r}>"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ decide

    async def decide(self, state: str, question_id: str) -> DecisionResult:
        """Ask ``question_id`` about ``state``. See the module docstring."""
        started_at = datetime.now()
        t0 = time.perf_counter()
        with self._tracer.start_as_current_span(SPAN_NAME) as span:
            progress = _Progress(started_at)
            code = await self._run(progress, state, question_id)
            result = self._finish(progress, code, t0)
            self._annotate(span, result)
        return result

    async def _run(
        self, p: _Progress, state: object, question_id: object
    ) -> str | None:
        """Run the pipeline; return an error code, or ``None`` on success."""
        if not self._base:
            return "disabled"
        question = get_question(question_id)
        if question is None or not isinstance(state, str):
            return "invalid_input"
        p.question = question
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._attest_identity(p)
                ids = await self.build_prompt_ids(state, question)
                p.input_sha256 = tpl.input_sha256(ids)
                await self._complete(p, question, ids)
        except DecisionError as exc:
            return exc.error_code
        except TimeoutError:
            return "timeout"
        except httpx.HTTPError as exc:
            return llama.map_transport_error(exc) or "malformed_response"
        except Exception as exc:
            # Closed vocabulary has no "internal" code; log the CLASS only
            # (never the message: it could carry user text).
            logger.warning("decision call failed unexpectedly: %s", type(exc).__name__)
            return "malformed_response"
        return None

    # ----------------------------------------------------------- pipeline

    async def _attest_identity(self, p: _Progress) -> None:
        if self._identity is None:
            identity = await llama.get_props(self._http, self._base)
            p.identity = identity
            if identity.model_alias != self._s.decision_model_alias:
                logger.warning("decision server model alias mismatch")
                raise DecisionError("model_identity_mismatch")
            self._identity = identity
        p.identity = self._identity

    async def _resolve_tables(self) -> _TokenTables:
        async with self._lock:
            if self._tables is not None:
                return self._tables
            special: dict[str, int] = {}
            denylist: set[int] = set()
            # Template specials are resolved independently of the denylist
            # (they are a subset of CONTROL_STRINGS today, but the two jobs
            # must not be coupled: the denylist is exactly CONTROL_STRINGS).
            for text in dict.fromkeys(
                tpl.CONTROL_STRINGS + tpl.TEMPLATE_SPECIAL_TOKENS
            ):
                ids = await llama.tokenize(
                    self._http, self._base, text, parse_special=True
                )
                if len(ids) == 1:
                    special[text] = ids[0]
                    if text in tpl.CONTROL_STRINGS:
                        denylist.add(ids[0])
            for text in tpl.TEMPLATE_SPECIAL_TOKENS:
                if text not in special:
                    raise DecisionError("template_token_unresolved")
            pieces: dict[str, list[int]] = {}
            for text in dict.fromkeys(tpl.TEMPLATE_TEXT_PIECES):
                pieces[text] = await llama.tokenize(
                    self._http, self._base, text, parse_special=False
                )
            self._tables = _TokenTables(special, pieces, frozenset(denylist))
            return self._tables

    async def _allowed_ids(
        self, tables: _TokenTables, n_options: int
    ) -> tuple[int, ...]:
        out: list[int] = []
        for letter in tpl.LETTERS[:n_options]:
            if letter not in tables.letter_ids:
                ids = await llama.tokenize(
                    self._http, self._base, letter, parse_special=False
                )
                if len(ids) != 1:
                    raise DecisionError("option_not_single_token")
                tables.letter_ids[letter] = ids[0]
            out.append(tables.letter_ids[letter])
        return tuple(out)

    async def build_prompt_ids(self, state: str, question: Question) -> list[int]:
        """The exact token-id prompt for ``(state, question)``.

        One ``parse_special:false`` call over the WHOLE user content, the
        denylist check, then the template splice. Public so the equality
        ``== /tokenize(render_choice_prompt(...), parse_special:true)`` can be
        asserted (T17 and the live probe).
        """
        tables = await self._resolve_tables()
        content = tpl.build_user_content(state, question.text, question.options)
        content_ids = await llama.tokenize(
            self._http, self._base, content, parse_special=False
        )
        if _carries_control_token(content, content_ids, tables.denylist):
            raise DecisionError("invalid_input")
        return tpl.splice_prompt_ids(tables.special_ids, tables.piece_ids, content_ids)

    async def _complete(self, p: _Progress, question: Question, ids: list[int]) -> None:
        tables = await self._resolve_tables()
        p.allowed_ids = await self._allowed_ids(tables, len(question.options))
        sampler = {
            "n_predict": 1,
            "n_probs": max(len(question.options), 20),
            "temperature": 0,
            "cache_prompt": False,
        }
        p.request = sampler
        p.raw = await llama.complete_top_logprobs(
            self._http, self._base, {"prompt": ids, **sampler}
        )
        try:
            dist = renormalise(p.raw, p.allowed_ids, self._s.decision_temperature)
        except ValueError as exc:
            raise DecisionError("no_allowed_token_in_top") from exc
        p.dist = tuple(dist)

    # ------------------------------------------------------------- output

    def _finish(self, p: _Progress, code: str | None, t0: float) -> DecisionResult:
        s = self._s
        q, ident = p.question, p.identity
        choice = confidence = ent = None
        if code is None and p.dist is not None:
            choice, confidence = choice_and_confidence(p.dist)
            ent = entropy(p.dist)
        return DecisionResult(
            result="ok" if code is None else result_for(code),
            error_code=code,
            started_at=p.started_at,
            latency_ms=int(round((time.perf_counter() - t0) * 1000)),
            choice_index=choice,
            confidence=confidence,
            entropy=ent,
            distribution=p.dist if code is None else None,
            raw_top_logprobs=p.raw if code is None else None,
            allowed_ids=p.allowed_ids if code is None else None,
            template_id=tpl.TEMPLATE_ID,
            template_sha256=tpl.TEMPLATE_SHA256,
            upstream_chat_template_sha256=tpl.UPSTREAM_CHAT_TEMPLATE_SHA256,
            question_id=q.question_id if q else None,
            question_sha256=q.question_sha256 if q else None,
            options_sha256=q.options_sha256 if q else None,
            input_sha256=p.input_sha256,
            decision_model=s.decision_model,
            decision_model_digest_configured=s.decision_model_digest,
            server_model_file=ident.model_file if ident else None,
            server_model_alias=ident.model_alias if ident else None,
            runtime=RUNTIME,
            runtime_version=ident.build_info if ident else None,
            backend=s.decision_backend,
            quantisation=s.decision_quantisation,
            temperature=s.decision_temperature,
            sampler_params=dict(p.request) if p.request else None,
            n_probs=p.request["n_probs"] if p.request else None,
        )

    @staticmethod
    def _annotate(span: Span, r: DecisionResult) -> None:
        """Span attributes: ids, hashes, enums and numbers ONLY (no text)."""
        attrs: dict[str, str | int | float | None] = {
            "template_id": r.template_id,
            "input_sha256": r.input_sha256,
            "result": r.result,
            "error_code": r.error_code,
            "choice_index": r.choice_index,
            "confidence": r.confidence,
            "latency_ms": r.latency_ms,
            "decision_model_digest_configured": r.decision_model_digest_configured,
        }
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(key, value)
