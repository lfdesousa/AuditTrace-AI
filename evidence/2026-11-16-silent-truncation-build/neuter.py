#!/usr/bin/env python3
"""Neuter every guard of the silent-truncation change and record which tests redden.

    cd <a checkout of the change> && PYTHONPATH=$PWD/src <venv>/bin/python \
        evidence/2026-11-16-silent-truncation-build/neuter.py --out <json> [--only K1,M2,...]

Each neuter is one or more exact textual replacements that must each match
exactly once, then the chain's tests run, then ``git checkout -- src bff``
restores the tree. The baseline before the first neuter and the run after the
last restore must both be 0 failed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TESTS = [
    "tests/test_memory_whole_document_complete.py",
    "tests/test_memory_write_limit.py",
    "tests/test_memory_upload_manifest_row.py",
    "tests/test_memory_whole_document_integrity.py",
    "tests/test_memory_documents_folded_before_the_stamp.py",
    "tests/test_memory_fold_tail_guards.py",
    "tests/test_memory_pdf_upload_bound.py",
    "tests/test_memory_request_body_ceiling.py",
    "tests/test_memory_write_tools_pin.py",
    "tests/test_memory_upload_refusals.py",
    "tests/test_memory_refusal_audit.py",
    "tests/bff/test_console_files_refusal_relay.py",
    "tests/test_memory_stamp_is_server_only.py",
    "tests/test_memory_verified_means_the_fold.py",
    "tests/test_memory_whitespace_documents_read_whole.py",
    "tests/test_memory_chunk_document.py",
    "tests/test_memory_pdf_upload_cap.py",
    "tests/bff/test_body_limit.py",
    "tests/test_memory_blank_refold_clears_the_document.py",
    "tests/test_wu1_memory_retrieval_correctness.py",
    "tests/test_body_limit_middlewares_asgi.py",
    "tests/test_memory_fold_tail_unit.py",
]
R = "src/audittrace/routes/"
MEMORY = R + "memory.py"
WHOLE = R + "memory_whole_document.py"
TAIL = R + "memory_fold_tail.py"
CHUNKING = R + "memory_chunking.py"
REFUSAL = R + "memory_write_refusal.py"
LAYER = R + "memory_layer_writes.py"
LIMIT = "src/audittrace/services/memory_write_limit.py"
MCP = "src/audittrace/tools/mcp_write_handlers.py"
SERVER = "src/audittrace/server.py"
MIDDLEWARE = "src/audittrace/memory_body_limit_middleware.py"
BFF_APP = "bff/app.py"
BFF_REFUSAL = "bff/body_limit_refusal.py"
BFF_MIDDLEWARE = "bff/body_limit_middleware.py"

Edit = tuple[str, str, str]
Neuter = tuple[str, str, list[Edit]]

_COUNT_ONLY = '        if read_stamp(candidate.metadata) != head:\n'
_COUNT_ONLY_NEW = '        if (read_stamp(candidate.metadata) or (0, ""))[0] != head[0]:\n'
_HASH = "    if text_sha256(reassemble_chunk_sequence(chunk_docs, overlap)) != head[1]:\n"
_HASH_NEW = "    if False and text_sha256(reassemble_chunk_sequence(chunk_docs, overlap)) != head[1]:\n"
_AUTH_LINE = '        await authorize_layer_write(manifest, user, layer.value, target_filename, tier)\n'

NEUTERS: list[Neuter] = [
    # -- the stamp, the read, the stale tail (M) --
    ("M1", "a stamped document without chunk 0 falls back to one chunk",
     [(WHOLE, "        if stamped:\n", "        if False and stamped:\n")]),
    ("M2", "chunks are matched on the chunk COUNT only, not the fold identity",
     [(WHOLE, _COUNT_ONLY, _COUNT_ONLY_NEW)]),
    ("M3", "the reassembled text is not hashed against the fold", [(WHOLE, _HASH, _HASH_NEW)]),
    ("M4", "count only AND no hash", [(WHOLE, _COUNT_ONLY, _COUNT_ONLY_NEW), (WHOLE, _HASH, _HASH_NEW)]),
    ("M5", "the tail walk ignores chunks stored at the pre-LC-9 id",
     [(TAIL, '            legacy = await collection.get(ids=logical, include=["metadatas"])\n',
       '            legacy = {"ids": [], "metadatas": []}\n')]),
    ("M6", "an unstamped document is reported verified",
     [(WHOLE, "        return chunk_ids, chunk_docs, False\n", "        return chunk_ids, chunk_docs, True\n")]),
    ("M7", "the stale tail is removed without authorization",
     [(TAIL, '            await authorize_write(\n                manifest_service, user, "semantic", key, requested_tier=tier\n            )\n',
       "            continue\n")]),
    ("M8", "the tail removal writes no audit event",
     [(TAIL, "            await emit_memory_audit_event(\n", "            await _no_audit(\n"),
      (TAIL, "logger = logging.getLogger(__name__)\n",
       "logger = logging.getLogger(__name__)\n\n\nasync def _no_audit(**_: Any) -> None:\n    return None\n")]),
    ("M9", "the first window is sized by the stamp the caller supplied",
     [(WHOLE, "        stop = start + CHUNK_READ_WINDOW\n",
       "        stop = start + (requested[0] if requested else CHUNK_READ_WINDOW)\n")]),
    ("M10", "a PDF is not bounded at upload",
     [(REFUSAL, "    refuse_oversize_or_413(content, pdf=pdf)\n", "    if not pdf:\n        refuse_oversize_or_413(content)\n")]),
    ("M11", "the claimed content type alone makes an upload a PDF",
     [(REFUSAL, '    pdf = is_pdf_upload(claimed_content_type=file.content_type or "", content=head)\n',
       '    pdf = (file.content_type or "").startswith("application/pdf")\n')]),
    ("M12", "the request-body ceiling is not installed",
     [(SERVER, "    app.add_middleware(MemoryBodyLimitMiddleware)\n", "")]),
    ("M13", "only a declared Content-Length is checked, a stream is not counted",
     [(MIDDLEWARE, "                if received > ceiling:\n", "                if False and received > ceiling:\n")]),
    ("M14", "a third MCP write tool is registered with no over-limit case",
     [(MCP, "async def write_skill(\n",
       "async def write_skill_extra(user_context: UserContext, args: dict[str, Any]) -> dict[str, Any]:\n"
       "    return {}\n\n\n"
       'register_mcp_write_tool(name="write_note", description="x", parameters_schema={},\n'
       '                        required_scope="memory:skills:write")(write_skill_extra)\n\n\n'
       "async def write_skill(\n")]),
    ("M15", "the upload does not record what it stored",
     [(MEMORY, "    if layer != MemoryLayer.session:\n        await record_layer_write(\n",
       "    if False:\n        await record_layer_write(\n")]),
    ("M16", "the upload is not authorized before it writes",
     [(MEMORY, "    if layer != MemoryLayer.session:\n" + _AUTH_LINE,
       "    if False:\n" + _AUTH_LINE)]),
    # -- the first round's guards (N) --
    ("N1", "the read stops after the first window (the cap of the base)",
     [(WHOLE, "        if head is not None and len(chunk_ids) >= head[0]:\n            complete = True\n",
       "        complete = True\n")]),
    ("N2", "the fold no longer stamps its chunks", [(MEMORY, "                **stamp,\n", "")]),
    ("N3", "a short read is returned instead of refused",
     [(WHOLE, "    if same_fold != head[0]:\n", "    if False and same_fold != head[0]:\n")]),
    ("N4", "the size check accepts everything", [(LIMIT, "    if size > limit:\n", "    if False and size > limit:\n")]),
    ("N5", "the bound counts characters, not bytes",
     [(LIMIT, '    size = len(document if isinstance(document, bytes) else document.encode("utf-8"))\n',
       "    size = len(document)\n")]),
    ("N6", "ONE route loses its check (PUT /memory/episodic)",
     [(MEMORY, '    refuse_oversize_or_413(content)\n    title = payload.get("title")\n    service = get_episodic_service()\n',
       '    title = payload.get("title")\n    service = get_episodic_service()\n')]),
    ("N7", "the upload read does not refuse (the document is stored)",
     [(REFUSAL, "    refuse_oversize_or_413(content, pdf=pdf)\n", "")]),
    ("N8", "the MCP write tools lose their check", [(MCP, "        check_document_size(text)\n", "        pass\n")]),
    ("N9", "a shorter re-fold keeps its stale tail chunks",
     [(TAIL, "        if not found:\n            return StaleTail(col_name, filename, tuple(stale))\n",
       "        if True:\n            return StaleTail(col_name, filename, tuple(stale))\n")]),
    ("N10", "a NEW memory write route is added with no test case",
     [(MEMORY, '@router.post("/promote")\n',
       '@router.post("/episodic-import")\nasync def _unbounded_new_route() -> dict[str, str]:\n'
       '    return {}\n\n\n@router.post("/promote")\n')]),
    # -- the second round's guards (K) --
    ("K1", "the REST semantic writes keep a caller's stamp",
     [(MEMORY, '    {"tier", "user_id", "id_form", STAMP_TOTAL, STAMP_FOLD}\n', '    {"tier", "user_id", "id_form"}\n')]),
    ("K2", "the MCP write tools keep a caller's stamp",
     [(MCP, '    {"tier", "user_id", "id_form", "chunk_total", "fold_sha256"}\n', '    {"tier", "user_id", "id_form"}\n')]),
    ("K3", "the fold chunks with _chunk_text again (a whitespace window is dropped)",
     [(MEMORY, "        chunks = chunk_document(content, CHUNK_SIZE, CHUNK_OVERLAP)\n", "        chunks = _chunk_text(content)\n")]),
    ("K4", "chunk_document drops a whitespace window instead of attaching it",
     [(CHUNKING, "        elif spans:\n            spans[-1][1] = end\n", "        elif spans:\n            pass\n")]),
    ("K5", "the upload is read in two pieces and joined (a second copy)",
     [(REFUSAL, "    content = await file.read(bound + 1)\n",
       "    content = await file.read(4097)\n    content += await file.read(bound + 1 - len(content))\n")]),
    ("K6", "the PDF upload cap is ignored (only the fold's bound applies)",
     [(LIMIT, "    return min(settings.pdf_upload_max_size_mb, settings.pdf_max_size_mb) * 1024 * 1024\n",
       "    return settings.pdf_max_size_mb * 1024 * 1024\n")]),
    ("K7", "the BFF body ceiling is not installed", [(BFF_APP, "    app.add_middleware(BodyLimitMiddleware)\n", "")]),
    ("K8", "a write that lost the manifest race is not audited",
     [(LAYER, '            detail_extra={"manifest": "concurrent_write"},\n        )\n        raise',
       '            detail_extra={"manifest": "concurrent_write"},\n        ) if False else None\n        raise')]),
    ("K9", "POST /memory/episodic refuses an over-limit document only after it has stored and audited it",
     [(MEMORY, '    _validate_filename_or_400(filename, "episodic")\n    refuse_oversize_or_413(content)\n',
       '    _validate_filename_or_400(filename, "episodic")\n'),
      (MEMORY, '    entry = await record_layer_write(\n        manifest, user, "episodic", filename, content, title=title, tier=tier\n    )\n',
       '    entry = await record_layer_write(\n        manifest, user, "episodic", filename, content, title=title, tier=tier\n    )\n    refuse_oversize_or_413(content)\n')]),
    # -- this build's conditions: S14 (blank re-fold) and S15 (BFF 413 shape) --
    ("S14a", "a blank fold leaves the previous fold's chunks in place",
     [(MEMORY, "            await stale.authorize(manifest_service, user, tier)\n            await stale.drop(\n                collection,\n                manifest_service,\n                user,\n                caller_can_write_shared=caller_can_write_shared,\n            )\n            continue\n",
       "            continue\n")]),
    ("S14b", "a blank fold removes the tail without authorizing it",
     [(MEMORY, "            await stale.authorize(manifest_service, user, tier)\n            await stale.drop(\n                collection,\n                manifest_service,\n                user,\n                caller_can_write_shared=caller_can_write_shared,\n            )\n            continue\n",
       "            await stale.drop(\n                collection,\n                manifest_service,\n                user,\n                caller_can_write_shared=caller_can_write_shared,\n            )\n            continue\n")]),
    ("S14c", "a blank fold is looked up with the old 'kept' of a non-blank fold (tail starts at 1)",
     [(MEMORY, "collection, user, tier, col_name, obj[\"filename\"], kept=len(chunks)\n", "collection, user, tier, col_name, obj[\"filename\"], kept=max(len(chunks), 1)\n")]),
    ("S16a", "the BFF middleware stacks a 413 on a response the app already began",
     [(BFF_MIDDLEWARE, "            if answered:\n                raise\n", "            if False:\n                raise\n")]),
    ("S16b", "the memory middleware stacks a 413 on a response the app already began",
     [(MIDDLEWARE, "            if answered:\n                raise\n", "            if False:\n                raise\n")]),
    ("S16c", "the BFF middleware lets an app that swallowed the abort answer 400",
     [(BFF_MIDDLEWARE, "            if overflowed:\n                if not answered:\n", "            if False:\n                if not answered:\n")]),
    ("S16d", "the memory middleware lets an app that swallowed the abort answer 400",
     [(MIDDLEWARE, "            if overflowed:\n                if not answered:\n", "            if False:\n                if not answered:\n")]),
    ("S17a", "a fold audit that cannot be written is swallowed instead of a 500",
     [(TAIL, "        except Exception as exc:\n            raise HTTPException(\n", "        except ZeroDivisionError as exc:\n            raise HTTPException(\n")]),
    ("S17b", "the legacy-row walk takes a row that belongs to someone else",
     [(TAIL, '                if (meta or {}).get("user_id") == user.user_id and (meta or {}).get(\n                    "tier"\n                ) == "private":\n', "                if True:\n")]),
    ("S15a", "the BFF 413 loses the additive detail key",
     [(BFF_REFUSAL, '        "detail": message,\n', "")]),
    ("S15b", "the BFF 413 is the old detail-only body (no error object)",
     [(BFF_REFUSAL, '        "error": {\n            "message": message,\n            "type": REQUEST_TOO_LARGE_TYPE,\n            "param": None,\n            "code": REQUEST_TOO_LARGE_CODE,\n            "status": REQUEST_TOO_LARGE_STATUS,\n        },\n', "")]),
    ("S15c", "the BFF 413 error object drops its numeric status",
     [(BFF_REFUSAL, '            "status": REQUEST_TOO_LARGE_STATUS,\n', "")]),
]


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"neuter anchor matches {text.count(old)} times in {path}: {old!r}")
    file.write_text(text.replace(old, new))


def run_tests() -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-p", "no:cacheprovider", "--no-cov", "-q", "-rf", "--tb=no"],
        capture_output=True, text=True, check=False,
    )
    failed = sorted(re.findall(r"^FAILED (\S+)", proc.stdout, flags=re.M))
    tail = [line for line in proc.stdout.splitlines() if re.search(r"\d+ (passed|failed)", line)]
    return {"rc": proc.returncode, "failed": failed, "failed_count": len(failed),
            "summary": tail[-1].strip("= ") if tail else proc.stdout[-300:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--only", default="")
    ap.add_argument("--check-anchors", action="store_true", help="apply and restore each neuter without running tests")
    args = ap.parse_args()
    wanted = {x for x in args.only.split(",") if x}
    results: dict[str, object] = {}
    if not args.check_anchors:
        results["baseline"] = run_tests()
    for ident, what, edits in NEUTERS:
        if wanted and ident not in wanted:
            continue
        try:
            for path, old, new in edits:
                replace_once(path, old, new)
            if args.check_anchors:
                print(ident, "anchors ok", flush=True)
                continue
            results[ident] = {"what": what, "files": sorted({e[0] for e in edits}), **run_tests()}
        finally:
            subprocess.run(["git", "checkout", "-q", "--", "src", "bff"], check=True)
        if not args.check_anchors:
            print(ident, results[ident], flush=True)  # type: ignore[index]
    if not args.check_anchors:
        results["restored"] = run_tests()
        args.out.write_text(json.dumps(results, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
