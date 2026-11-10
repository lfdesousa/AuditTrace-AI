"""Logging for the BFF process (LC-10a review r8, F1).

The pod runs a bare ``uvicorn bff.app:app``; uvicorn configures only its own
``uvicorn*`` loggers, so every ``bff.*`` logger fell back to the root default
(WARNING) and its INFO records, such as ``token_exchange.scopes granted=``,
were dropped. ``Settings.log_level`` existed but was read nowhere.

``configure_logging`` is called from ``create_app`` and attaches one stdout
handler to the package logger ``bff``, so module-scoped ``bff.*`` loggers
inherit the level and print level and logger name. It is idempotent
(``create_app`` runs more than once in tests) and fails closed on an unknown
level. Names only: this module never formats a token.
"""

from __future__ import annotations

import logging
import sys

_PACKAGE_LOGGER = "bff"
_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_HANDLER_MARK = "_bff_stdout_handler"


def configure_logging(level: str) -> None:
    """Set the ``bff`` package logger to ``level`` with one stdout handler."""
    numeric = logging.getLevelNamesMapping().get(level.upper())
    if numeric is None:
        raise ValueError(f"unknown log_level {level!r}")
    package = logging.getLogger(_PACKAGE_LOGGER)
    package.setLevel(numeric)
    if any(getattr(h, _HANDLER_MARK, False) for h in package.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    setattr(handler, _HANDLER_MARK, True)
    package.addHandler(handler)
