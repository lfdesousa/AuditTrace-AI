"""Non-``/tmp`` evidence-dir helper for the neuter harness's own tests.

SPEC v3 §7 refuses ``/tmp`` (and ``tempfile.gettempdir()``) as an evidence
dir -- correctly: results must be durable, not routinely reaped. pytest's
own ``tmp_path`` fixture is ALWAYS rooted under ``tempfile.gettempdir()``,
so any test that needs a REAL (non-refused) evidence dir maps its
``tmp_path`` to a parallel directory under this repo's own gitignored
scratch space instead -- everything else (throwaway git repos, spec files,
fixture copies) stays under ``tmp_path`` exactly as before; only the
evidence dir itself needs to be outside ``/tmp``.

Prefixed with ``_`` so pytest never collects it as a test module.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / ".neuter_test_evidence"


def evidence_dir_for(tmp_path: Path, name: str = "ev", *, create: bool = True) -> Path:
    """A real (non-``/tmp``) evidence dir unique to this ``tmp_path`` +
    ``name`` -- pytest's per-test ``tmp_path`` is already collision-free,
    so mapping through its own name keeps this collision-free too.

    ``create=False`` returns the path WITHOUT making it -- needed by the
    proofs that assert a fail-closed path never created an evidence dir at
    all (creating the parent ``ROOT`` as a side effect of merely computing
    the path would silently defeat that assertion)."""
    d = ROOT / tmp_path.name / name
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_root() -> None:
    shutil.rmtree(ROOT, ignore_errors=True)
