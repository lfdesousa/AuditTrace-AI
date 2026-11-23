"""LIBRECHAT-IMAGE-SLICE AC-10 -- an offline (``--sql``) downgrade of migration 040 fails closed.

The guard has to COUNT the rows that record an image; a script cannot. So the offline
downgrade must refuse by name, never emit an unguarded ``DROP`` of ``input_media``.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config

REVISION = "ae606b421cf9:5b4bd766374f"


def test_an_offline_downgrade_refuses_and_emits_no_drop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", "sqlite://")
    with pytest.raises(RuntimeError, match="offline"):
        command.downgrade(cfg, REVISION, sql=True)
    emitted = capsys.readouterr().out.lower()
    assert "downgrade ae606b421cf9 -> 5b4bd766374f" in emitted, "control: the step ran"
    assert "drop" not in emitted
    assert "input_media" not in emitted.replace("downgrade ae606b421cf9", "")
