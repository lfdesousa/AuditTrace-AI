"""``pytest11`` entry point for the neuter chokepoint plugin (issue #368).

Registered in ``pyproject.toml`` as ``neuter_pathcheck``. pytest imports THIS
module at startup in every run that uses the project venv, whatever the
caller's environment (no ``PYTEST_PLUGINS`` needed), and ``pytest_configure``
delegates to ``scripts/neuter/neuter_pathcheck.py``, which stays the single
implementation. The real module is loaded lazily, inside ``pytest_configure``,
so it is imported after coverage has started and stays measured by the
per-file gate.

The plugin is inert unless the harness is active (see
``neuter_pathcheck.enforcement_active``), so ``make test`` is unaffected.

An editable (dev) install finds ``scripts/`` next to ``src/``. A non-editable
install (the runtime image has no pytest and no ``scripts/``) has nothing to
delegate to and does nothing.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

#: ``<repo>/scripts/neuter/neuter_pathcheck.py`` relative to ``<repo>/src``.
_REAL_PLUGIN = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "neuter"
    / "neuter_pathcheck.py"
)
_REAL_NAME = "neuter_pathcheck"


def _load_real_plugin() -> ModuleType | None:
    """Return the real plugin module, or ``None`` when it is not shipped."""
    existing = sys.modules.get(_REAL_NAME)
    if existing is not None:
        return existing
    if not _REAL_PLUGIN.is_file():
        logger.debug("neuter_pathcheck: %s absent; entry point inert", _REAL_PLUGIN)
        return None
    spec = importlib.util.spec_from_file_location(_REAL_NAME, _REAL_PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_REAL_NAME] = module
    spec.loader.exec_module(module)
    return module


def pytest_configure(config: Any) -> None:
    module = _load_real_plugin()
    if module is not None:
        module.pytest_configure(config)
