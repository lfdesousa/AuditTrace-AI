"""Structural tests on the Dockerfile.

Lightweight (parse-and-assert) tests that catch regressions in Docker
image build steps with load-bearing runtime implications.

Real image-build verification needs `docker build` and is too slow for
the per-commit test gate; it lives in `make k8s-build` instead.
"""

from __future__ import annotations

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


class TestNoInProcessEmbeddingModel:
    """ADR-047 (2026-06-19): embedding runs on the dedicated nomic-embed-
    server, so the gateway image must NOT bake ChromaDB's stock
    all-MiniLM-L6-v2 ONNX model. The old build step (`DefaultEmbeddingFunction()`
    pre-warm, ~79 MB) is removed — keep it removed so the gateway stays
    model-free and the image stays slim.
    """

    def test_dockerfile_does_not_prewarm_onnx_model(self) -> None:
        content = DOCKERFILE.read_text()
        assert "DefaultEmbeddingFunction()" not in content, (
            "Dockerfile re-introduced the in-process ChromaDB embedding "
            "model bake — ADR-047 moved embedding to the nomic server; "
            "the gateway must host no ONNX model."
        )
