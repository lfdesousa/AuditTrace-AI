"""The BFF configures its own logging from ``Settings.log_level`` (LC-10a
review r8, F1). Before this the pod ran a bare ``uvicorn bff.app:app`` and
every ``bff.*`` INFO record (the ``token_exchange.scopes granted=`` success
line) fell back to the root WARNING default and was dropped.

These tests deliberately do NOT call ``caplog.set_level``: that is the
condition the r7 tests configured away."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterator

import pytest

from bff.app import create_app
from bff.logging_setup import configure_logging
from tests.bff.test_exchange_scope_log import _exchange


@pytest.fixture
def pristine_bff_logger() -> Iterator[logging.Logger]:
    """Return the ``bff`` logger as an unconfigured process would have it,
    and put it back afterwards."""
    log = logging.getLogger("bff")
    root = logging.getLogger()
    saved = (log.level, list(log.handlers), log.propagate)
    saved_root_level = root.level
    # uvicorn's default config leaves the root logger at WARNING; other
    # suites in this process may have raised it, which would hide F1.
    root.setLevel(logging.WARNING)
    log.setLevel(logging.NOTSET)
    log.handlers.clear()
    yield log
    root.setLevel(saved_root_level)
    log.setLevel(saved[0])
    log.handlers[:] = saved[1]
    log.propagate = saved[2]


class TestPodLoggingEmitsTheScopeLine:
    async def test_create_app_makes_the_info_granted_line_emit(
        self,
        caplog: pytest.LogCaptureFixture,
        pristine_bff_logger: logging.Logger,
    ) -> None:
        create_app()  # what ``bff.app:app`` does at import, in the pod
        await _exchange("audittrace:query", None)
        granted = [
            r
            for r in caplog.records
            if r.name == "bff.exchange"
            and r.getMessage() == "token_exchange.scopes granted=audittrace:query"
        ]
        assert len(granted) == 1
        assert granted[0].levelno == logging.INFO

    def test_the_deployed_import_path_prints_level_and_logger_name_to_stdout(
        self,
    ) -> None:
        """Fresh interpreter, uvicorn's import path, no pytest logging."""
        code = (
            "import logging\n"
            "import bff.app\n"
            "logging.getLogger('bff.exchange').info("
            "'token_exchange.scopes granted=a,b')\n"
            "logging.getLogger('bff.exchange').warning('w')\n"
        )
        env = {
            **os.environ,
            "AUDITTRACE_BFF_ENV": "test",
            "AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET": "test-secret-never-real",
        }
        env.pop("AUDITTRACE_BFF_LOG_LEVEL", None)
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        lines = proc.stdout.splitlines()
        assert any(
            "INFO" in line
            and "bff.exchange" in line
            and line.endswith("token_exchange.scopes granted=a,b")
            for line in lines
        ), proc.stdout + proc.stderr
        assert any("WARNING" in line and "bff.exchange" in line for line in lines)


class TestConfigureLogging:
    def test_idempotent_one_handler(self, pristine_bff_logger: logging.Logger) -> None:
        configure_logging("INFO")
        configure_logging("INFO")
        assert len(pristine_bff_logger.handlers) == 1

    def test_level_follows_the_setting(
        self, pristine_bff_logger: logging.Logger
    ) -> None:
        configure_logging("WARNING")
        assert not logging.getLogger("bff.exchange").isEnabledFor(logging.INFO)
        configure_logging("debug")
        assert logging.getLogger("bff.exchange").isEnabledFor(logging.DEBUG)

    def test_an_unknown_level_fails_closed(
        self, pristine_bff_logger: logging.Logger
    ) -> None:
        with pytest.raises(ValueError, match="log_level"):
            configure_logging("LOUD")
