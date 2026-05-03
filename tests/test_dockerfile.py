"""Structural tests on the Dockerfile.

Lightweight (parse-and-assert) tests that catch regressions in Docker
image build steps that have load-bearing implications at runtime —
specifically the ChromaDB embedding-model pre-warm baked into the
image layer (PR A live-test finding, 2026-05-03).

Real image-build verification needs `docker build` and is too slow for
the per-commit test gate; it lives in `make k8s-build` instead. These
tests are the cheap-fast belt that catches "someone refactored the
Dockerfile and forgot the prewarm" before it lands in main.
"""

from __future__ import annotations

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


class TestChromaDBEmbeddingPrewarm:
    """The runtime image MUST pre-warm ChromaDB's default sentence-
    transformers embedding model at build time. Without this, the
    FIRST `collection.upsert()` call from any pod triggers an
    in-process download of the 79 MB ONNX model that blocks the
    FastAPI worker for ~26 s. Long enough for kubelet's liveness
    probe to mark the pod unhealthy, kill it mid-request, and trip
    a CrashLoopBackOff.

    The fix is to bake the model into the image at build time so
    every pod boots with the cache hot. ~79 MB extra image size
    — acceptable trade for not killing pods on first semantic POST.
    """

    def test_dockerfile_runs_default_embedding_function_at_build(self) -> None:
        """The build stage must invoke `DefaultEmbeddingFunction()` so
        the ONNX download materialises into the image layer."""
        content = DOCKERFILE.read_text()
        assert "DefaultEmbeddingFunction" in content, (
            "Dockerfile no longer pre-warms ChromaDB embedding model — "
            "every pod will pay the 79 MB download cost on first "
            "semantic upsert and risk being killed mid-request."
        )

    def test_prewarm_runs_with_explicit_home(self) -> None:
        """The prewarm RUN must set HOME=/home/sovereign so chromadb
        caches the ONNX model where the runtime user can read it,
        not in /root/.cache where it would be unreachable after the
        USER switch.
        """
        content = DOCKERFILE.read_text()
        assert "HOME=/home/sovereign" in content, (
            "prewarm RUN doesn't set HOME — model caches under root's "
            "home and the runtime sovereign user can't read it"
        )

    def test_prewarm_chowns_cache_to_runtime_user(self) -> None:
        """The cached model directory must end up owned by uid:gid
        1000:1000 (the runtime sovereign user) so it's readable by
        the running pod."""
        content = DOCKERFILE.read_text()
        assert "chown -R 1000:1000 /home/sovereign/.cache" in content, (
            "prewarm RUN doesn't chown the cache — pod won't be able "
            "to read the cached model and will fall back to download"
        )
