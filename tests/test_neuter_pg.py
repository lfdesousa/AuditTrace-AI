"""Unit tests for ``scripts/neuter/pg.py`` -- non-durable Postgres, the
fake-mode seam, settings read-back, and catalog isolation (SPEC v3 §8, §9)."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time

import pytest

from scripts.neuter.pg import (
    NonDurableSettingsError,
    _wait_ready,
    assert_nondurable,
    db_snapshot,
    foreign_product_pg_container_running,
    free_port,
    poll_db_leak,
    read_settings,
    start_container,
    stop_container,
)

_DOCKER_AVAILABLE = shutil.which("docker") is not None
if _DOCKER_AVAILABLE:
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
    except Exception:
        _DOCKER_AVAILABLE = False

requires_docker = pytest.mark.skipif(not _DOCKER_AVAILABLE, reason="docker unavailable")


def test_free_port_returns_usable_port():
    port = free_port()
    assert 0 < port < 65536


def test_fake_start_container_reports_nondurable_settings(tmp_path):
    handle = start_container("r1", 1, fake=True, fake_dir=tmp_path)
    assert handle.fake is True
    settings = read_settings(handle)
    assert settings == {
        "fsync": "off",
        "synchronous_commit": "off",
        "full_page_writes": "off",
    }
    assert_nondurable(settings)  # does not raise


def test_fake_settings_violation_raises(tmp_path):
    handle = start_container("r1", 1, fake=True, fake_dir=tmp_path)
    state = json.loads(handle.fake_state_path.read_text())
    state["settings"]["synchronous_commit"] = "on"
    handle.fake_state_path.write_text(json.dumps(state))
    with pytest.raises(NonDurableSettingsError):
        assert_nondurable(read_settings(handle))


def test_assert_nondurable_reads_all_three_fields():
    with pytest.raises(NonDurableSettingsError):
        assert_nondurable(
            {"fsync": "off", "synchronous_commit": "off", "full_page_writes": "on"}
        )


def test_db_snapshot_and_leak_detection_fake(tmp_path):
    handle = start_container("r1", 1, fake=True, fake_dir=tmp_path)
    before = db_snapshot(handle)
    assert before == (frozenset(), 0)
    # simulate a leaked schema
    state = json.loads(handle.fake_state_path.read_text())
    state["schemata"].append("leaked")
    handle.fake_state_path.write_text(json.dumps(state))
    leaked = poll_db_leak(handle, before, deadline_s=0.3, interval_s=0.05)
    assert leaked is True


def test_db_snapshot_no_leak_when_unchanged(tmp_path):
    handle = start_container("r1", 1, fake=True, fake_dir=tmp_path)
    before = db_snapshot(handle)
    leaked = poll_db_leak(handle, before, deadline_s=0.2, interval_s=0.05)
    assert leaked is False


def test_stop_container_fake_is_noop(tmp_path):
    handle = start_container("r1", 1, fake=True, fake_dir=tmp_path)
    stop_container(handle)  # must not raise, must not touch docker


def test_wait_ready_times_out_on_an_unreachable_dsn():
    """Real ``create_engine``/``connect`` against a closed port -- exercises
    the retry-then-``TimeoutError`` path without needing a live container."""
    bad_dsn = "postgresql+psycopg2://postgres:x@127.0.0.1:1/nosuchdb"
    with pytest.raises(TimeoutError):
        _wait_ready(bad_dsn, timeout_s=0.3)


@requires_docker
def test_real_container_lifecycle_settings_and_catalog():
    """Real ``postgres:16`` end-to-end: start (non-fake), SHOW read-back,
    catalog snapshot, stop -- the code path the self-proofs' ``fake=True``
    seam stands in for."""
    handle = start_container("t-pg-real", 99)
    try:
        settings = read_settings(handle)
        assert settings == {
            "fsync": "off",
            "synchronous_commit": "off",
            "full_page_writes": "off",
        }
        assert_nondurable(settings)  # does not raise
        schemata, sessions = db_snapshot(handle)
        assert schemata == frozenset()
        assert sessions >= 0
        assert (
            poll_db_leak(handle, (schemata, sessions), deadline_s=0.3, interval_s=0.05)
            is False
        )
    finally:
        stop_container(handle)


def test_poll_db_leak_actually_repolls_and_catches_a_recovery(tmp_path):
    """ESC-3 (review round 1): the reviewer's escape drops the
    ``while ...:`` re-check entirely (``while False and ...``), so
    ``poll_db_leak`` would only ever look ONCE, at t=0. Real polling must
    catch a catalog that's dirty at t=0 but RECOVERS (a schema dropped by a
    slightly-delayed cleanup) before the deadline -- proven here with a
    background thread that clears the leaked schema mid-poll, independent
    of anything ``run_one_neuter`` orchestrates."""
    handle = start_container("esc3", 1, fake=True, fake_dir=tmp_path)
    before = db_snapshot(handle)
    state = json.loads(handle.fake_state_path.read_text())
    state["schemata"].append("leaked_schema")
    handle.fake_state_path.write_text(json.dumps(state))

    def _recover() -> None:
        time.sleep(0.12)
        recovered = json.loads(handle.fake_state_path.read_text())
        recovered["schemata"] = []
        handle.fake_state_path.write_text(json.dumps(recovered))

    threading.Thread(target=_recover, daemon=True).start()
    # dirty at t=0, recovers ~0.12s in -- a real poll (interval < recovery
    # time, deadline > recovery time) must observe the recovery and report
    # NO leak; a disabled loop would report the t=0 reading forever.
    leaked = poll_db_leak(handle, before, deadline_s=0.6, interval_s=0.05)
    assert leaked is False


def test_foreign_product_pg_container_running_false_when_none(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert foreign_product_pg_container_running() is False


def test_foreign_product_pg_container_running_true_for_a_match(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout="audittrace-acl-wu2a-pg-12345\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert foreign_product_pg_container_running() is True


def test_foreign_product_pg_container_running_false_on_nonzero_exit(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="no docker")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert foreign_product_pg_container_running() is False


@requires_docker
def test_start_container_real_with_tmpfs():
    """The ``tmpfs=True`` branch of the real (non-fake) container path."""
    handle = start_container("t-pg-tmpfs", 98, tmpfs=True)
    try:
        settings = read_settings(handle)
        assert_nondurable(settings)  # does not raise
    finally:
        stop_container(handle)
