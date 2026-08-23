"""Tests for the RETENTION drift-check (``scripts/retention-drift-check.py``,
SPEC ``2026-08-22-SPEC-retention-per-signal-windows``).

The module lives outside ``src/`` (a repo-level operator tool, deliberately
OFF the ADR-049 heavy per-file coverage gate per the spec), so it is loaded
by file path rather than imported as a package — same convention as
``tests/test_coverage_gate.py`` / ``tests/test_adr049_evidence_check.py``.

**CI-safety (non-negotiable per the spec):** none of these tests depend on
the real sibling obs-stack checkout (``~/work/observability-stack``) being
present on the machine running the suite — every "live obs-stack" scenario
below is built from a hermetic ``tmp_path`` fixture that mirrors the real
obs-stack's file layout (``tempo/tempo.yml``, ``loki/loki-config.yml``,
``docker-compose.yml``). This is what makes ``make test`` green whether or
not an operator happens to have the sibling repo checked out.

**Non-vacuous proof (falsifiable acceptance, spec Rule 1):**
``TestNonVacuousRedGreenProof`` hand-edits a fixture obs-stack config to a
WRONG window (drift-check goes RED / exit 1), then restores the correct
window (drift-check goes GREEN / exit 0) — both directions, proving the
guard actually detects the regression it claims to guard against rather
than always passing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "retention-drift-check.py"
)
_SSOT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "charts"
    / "audittrace"
    / "files"
    / "retention-windows.yaml"
)

# The ratified table, verbatim from the RATIFIED spec
# (2026-08-22-SPEC-retention-per-signal-windows) — the operator's actual
# decision, independent of whatever the SSOT YAML file currently says. This
# is the second, independent copy that makes ``test_ssot_matches_ratified_*``
# a real drift guard against the SSOT file itself silently diverging from
# what was ratified (e.g. an un-reviewed edit reverting a window back to a
# tool default), not just a self-referential no-op.
RATIFIED_WINDOWS = {
    "tempo_traces": "7d",
    "loki_logs": "30d",
    "prometheus_metrics": "30d",
    "minio_memory_objects": "no-expiry",
    "chromadb_semantic": "no-expiry",
    "postgres_audit_rows": "no-expiry",
}


def _load_module():
    spec = importlib.util.spec_from_file_location("retention_drift_check", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["retention_drift_check"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# --- SSOT config content -----------------------------------------------


class TestSsotMatchesRatifiedTable:
    """The SSOT YAML (``charts/audittrace/files/retention-windows.yaml``)
    must declare EXACTLY the operator-ratified windows — this is what
    stops the codified config from silently drifting away from the
    ratified decision (e.g. a future edit reverting a window without a new
    ratification)."""

    def test_all_ratified_signals_present(self, mod) -> None:
        config = mod.load_ratified_config(_SSOT_CONFIG_PATH)
        assert set(config["signals"].keys()) == set(RATIFIED_WINDOWS.keys())

    @pytest.mark.parametrize("signal_name", sorted(RATIFIED_WINDOWS.keys()))
    def test_declared_window_matches_ratified(self, mod, signal_name: str) -> None:
        config = mod.load_ratified_config(_SSOT_CONFIG_PATH)
        declared = config["signals"][signal_name]["window"]
        assert declared == RATIFIED_WINDOWS[signal_name], (
            f"{signal_name}: SSOT declares {declared!r}, ratified table says "
            f"{RATIFIED_WINDOWS[signal_name]!r} — the codified config has "
            "drifted from the operator's ratified decision."
        )

    def test_obs_stack_signals_have_parseable_source(self, mod) -> None:
        """Tempo/Loki/Prometheus must carry a live-parseable source kind —
        the drift-check can't actually check anything live for a signal
        without one."""
        config = mod.load_ratified_config(_SSOT_CONFIG_PATH)
        parseable = {"obs_stack_yaml", "obs_stack_compose_flag"}
        for name in ("tempo_traces", "loki_logs", "prometheus_metrics"):
            kind = config["signals"][name]["source"]["kind"]
            assert kind in parseable, f"{name}: source.kind={kind!r} not live-parseable"

    def test_durable_signals_are_declared_only(self, mod) -> None:
        """MinIO/ChromaDB/Postgres have no reachable live-check target from
        this repo (the bucket-lifecycle apply is operator-attended) — must
        be source.kind == 'declared'."""
        config = mod.load_ratified_config(_SSOT_CONFIG_PATH)
        for name in (
            "minio_memory_objects",
            "chromadb_semantic",
            "postgres_audit_rows",
        ):
            kind = config["signals"][name]["source"]["kind"]
            assert kind == "declared", (
                f"{name}: source.kind={kind!r}, expected 'declared'"
            )


# --- duration parsing -----------------------------------------------------


class TestParseDurationHours:
    @pytest.mark.parametrize(
        ("raw", "expected_hours"),
        [
            ("24h", 24.0),
            ("7d", 168.0),
            ("30d", 720.0),
            ("720h", 720.0),
            ("1w", 168.0),
            ("4w", 672.0),
        ],
    )
    def test_valid_durations(self, mod, raw: str, expected_hours: float) -> None:
        assert mod.parse_duration_hours(raw) == expected_hours

    def test_cross_unit_equivalence(self, mod) -> None:
        # 7d and 168h must compare equal — the guard must not false-positive
        # just because the live config and the SSOT use different units.
        assert mod.parse_duration_hours("7d") == mod.parse_duration_hours("168h")

    @pytest.mark.parametrize("bad", ["", "7", "d7", "7 days", "-1d", "no-expiry"])
    def test_unparseable_raises(self, mod, bad: str) -> None:
        with pytest.raises(mod.RetentionDriftError):
            mod.parse_duration_hours(bad)


# --- config loading ---------------------------------------------------


class TestLoadRatifiedConfig:
    def test_missing_file_raises(self, mod, tmp_path: Path) -> None:
        with pytest.raises(mod.RetentionDriftError):
            mod.load_ratified_config(tmp_path / "does-not-exist.yaml")

    def test_malformed_config_raises(self, mod, tmp_path: Path) -> None:
        bad = tmp_path / "retention-windows.yaml"
        bad.write_text("just: a random mapping\nwith_no_signals_key: true\n")
        with pytest.raises(mod.RetentionDriftError):
            mod.load_ratified_config(bad)


# --- obs-stack parsing --------------------------------------------------


def _write_reference_obs_stack(obs_stack_dir: Path) -> None:
    """A fixture obs-stack checkout with CORRECT (ratified) windows,
    mirroring the real sibling repo's file layout exactly (see
    ``tempo/tempo.yml``, ``loki/loki-config.yml``, ``docker-compose.yml``
    verified from ``~/work/observability-stack`` during the build)."""
    tempo_dir = obs_stack_dir / "tempo"
    tempo_dir.mkdir(parents=True)
    (tempo_dir / "tempo.yml").write_text(
        yaml.safe_dump(
            {
                "compactor": {"compaction": {"block_retention": "7d"}},
            }
        )
    )

    loki_dir = obs_stack_dir / "loki"
    loki_dir.mkdir(parents=True)
    (loki_dir / "loki-config.yml").write_text(
        yaml.safe_dump(
            {
                "limits_config": {
                    "reject_old_samples": True,
                    "reject_old_samples_max_age": "168h",
                    # Deliberately expressed in hours here to also prove the
                    # cross-unit comparison against the SSOT's "30d" form.
                    "retention_period": "720h",
                },
            }
        )
    )

    (obs_stack_dir / "docker-compose.yml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    "prometheus": {
                        "image": "prom/prometheus:latest",
                        "command": [
                            "--config.file=/etc/prometheus/prometheus.yml",
                            "--storage.tsdb.retention.time=30d",
                        ],
                    },
                },
            }
        )
    )


class TestObsStackParsing:
    def test_read_obs_stack_yaml_value_present(self, mod, tmp_path: Path) -> None:
        _write_reference_obs_stack(tmp_path)
        value = mod.read_obs_stack_yaml_value(
            tmp_path, "tempo/tempo.yml", ["compactor", "compaction", "block_retention"]
        )
        assert value == "7d"

    def test_read_obs_stack_yaml_value_missing_file(self, mod, tmp_path: Path) -> None:
        assert mod.read_obs_stack_yaml_value(tmp_path, "tempo/tempo.yml", ["a"]) is None

    def test_read_obs_stack_yaml_value_missing_pointer(
        self, mod, tmp_path: Path
    ) -> None:
        _write_reference_obs_stack(tmp_path)
        # loki-config.yml today (pre-ratification reality) has no compactor
        # block at all — the pointer walk must return None, not raise.
        value = mod.read_obs_stack_yaml_value(
            tmp_path, "loki/loki-config.yml", ["compactor", "retention_enabled"]
        )
        assert value is None

    def test_read_compose_flag_value_present(self, mod, tmp_path: Path) -> None:
        _write_reference_obs_stack(tmp_path)
        value = mod.read_compose_flag_value(
            tmp_path / "docker-compose.yml", "--storage.tsdb.retention.time"
        )
        assert value == "30d"

    def test_read_compose_flag_value_missing_flag(self, mod, tmp_path: Path) -> None:
        _write_reference_obs_stack(tmp_path)
        value = mod.read_compose_flag_value(
            tmp_path / "docker-compose.yml", "--unknown-flag"
        )
        assert value is None

    def test_read_compose_flag_value_missing_file(self, mod, tmp_path: Path) -> None:
        value = mod.read_compose_flag_value(
            tmp_path / "does-not-exist.yml", "--storage.tsdb.retention.time"
        )
        assert value is None


# --- check_signal / SignalResult ----------------------------------------


class TestCheckSignal:
    def test_declared_signal_never_checked_live(self, mod, tmp_path: Path) -> None:
        _write_reference_obs_stack(tmp_path)
        spec = {"window": "no-expiry", "source": {"kind": "declared"}}
        result = mod.check_signal("minio_memory_objects", spec, tmp_path)
        assert result.checked_live is False
        assert result.drifted is False

    def test_none_obs_stack_dir_short_circuits_to_declared(self, mod) -> None:
        spec = {
            "window": "7d",
            "source": {
                "kind": "obs_stack_yaml",
                "path": "tempo/tempo.yml",
                "pointer": ["compactor", "compaction", "block_retention"],
            },
        }
        result = mod.check_signal("tempo_traces", spec, None)
        assert result.checked_live is False
        assert result.drifted is False

    def test_matching_live_value_is_not_drift(self, mod, tmp_path: Path) -> None:
        _write_reference_obs_stack(tmp_path)
        spec = {
            "window": "7d",
            "source": {
                "kind": "obs_stack_yaml",
                "path": "tempo/tempo.yml",
                "pointer": ["compactor", "compaction", "block_retention"],
            },
        }
        result = mod.check_signal("tempo_traces", spec, tmp_path)
        assert result.checked_live is True
        assert result.effective == "7d"
        assert result.drifted is False

    def test_diverging_live_value_is_drift(self, mod, tmp_path: Path) -> None:
        _write_reference_obs_stack(tmp_path)
        (tmp_path / "tempo" / "tempo.yml").write_text(
            yaml.safe_dump({"compactor": {"compaction": {"block_retention": "24h"}}})
        )
        spec = {
            "window": "7d",
            "source": {
                "kind": "obs_stack_yaml",
                "path": "tempo/tempo.yml",
                "pointer": ["compactor", "compaction", "block_retention"],
            },
        }
        result = mod.check_signal("tempo_traces", spec, tmp_path)
        assert result.drifted is True

    def test_missing_live_value_is_drift(self, mod, tmp_path: Path) -> None:
        _write_reference_obs_stack(tmp_path)
        spec = {
            "window": "30d",
            "source": {
                "kind": "obs_stack_yaml",
                "path": "loki/loki-config.yml",
                "pointer": ["limits_config", "retention_period_typo"],
            },
        }
        result = mod.check_signal("loki_logs", spec, tmp_path)
        assert result.effective is None
        assert result.drifted is True

    def test_unknown_source_kind_raises(self, mod, tmp_path: Path) -> None:
        spec = {"window": "7d", "source": {"kind": "smoke-signal"}}
        with pytest.raises(mod.RetentionDriftError):
            mod.check_signal("tempo_traces", spec, tmp_path)


# --- run_drift_check / CLI end-to-end -----------------------------------


def _write_ssot(config_path: Path) -> None:
    config_path.write_text(
        yaml.safe_dump(
            {
                "signals": {
                    "tempo_traces": {
                        "window": "7d",
                        "source": {
                            "kind": "obs_stack_yaml",
                            "path": "tempo/tempo.yml",
                            "pointer": ["compactor", "compaction", "block_retention"],
                        },
                    },
                    "loki_logs": {
                        "window": "30d",
                        "source": {
                            "kind": "obs_stack_yaml",
                            "path": "loki/loki-config.yml",
                            "pointer": ["limits_config", "retention_period"],
                        },
                    },
                    "prometheus_metrics": {
                        "window": "30d",
                        "source": {
                            "kind": "obs_stack_compose_flag",
                            "path": "docker-compose.yml",
                            "flag": "--storage.tsdb.retention.time",
                        },
                    },
                    "minio_memory_objects": {
                        "window": "no-expiry",
                        "source": {"kind": "declared"},
                    },
                }
            }
        )
    )


class TestGracefulSkipWhenObsStackAbsent:
    """CI-SAFE path — mirrors ``make check-dashboard-drift``: exit 0 +
    a clear notice, no dependence on the sibling repo being checked out."""

    def test_exit_zero_and_notice(self, mod, tmp_path: Path, capsys) -> None:
        config_path = tmp_path / "retention-windows.yaml"
        _write_ssot(config_path)
        absent_dir = tmp_path / "no-such-obs-stack"

        exit_code = mod.run_drift_check(config_path, absent_dir)

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "obs-stack not present, skipping" in captured.out
        assert str(absent_dir) in captured.out

    def test_declared_signal_still_echoed(self, mod, tmp_path: Path, capsys) -> None:
        """Even with the obs-stack absent, the declared (MinIO/ChromaDB/
        Postgres-shaped) signals still print their ratified window for the
        audit trail — they never depend on obs-stack presence."""
        config_path = tmp_path / "retention-windows.yaml"
        _write_ssot(config_path)

        mod.run_drift_check(config_path, tmp_path / "absent")

        captured = capsys.readouterr()
        assert "minio_memory_objects" in captured.out
        assert "no-expiry" in captured.out


class TestNonVacuousRedGreenProof:
    """Falsifiable acceptance (spec Rule 1): hand-edit a live obs-stack
    config to a WRONG window -> RED (exit 1); restore -> GREEN (exit 0).
    Both directions are proven in one test so a change that makes the
    guard permanently pass (or permanently fail) is caught immediately."""

    def test_wrong_window_then_restored(self, mod, tmp_path: Path, capsys) -> None:
        config_path = tmp_path / "retention-windows.yaml"
        _write_ssot(config_path)
        obs_stack_dir = tmp_path / "observability-stack"
        _write_reference_obs_stack(obs_stack_dir)

        # 1. GREEN — the fixture obs-stack matches the ratified table.
        exit_code = mod.run_drift_check(config_path, obs_stack_dir)
        green_output = capsys.readouterr().out
        assert exit_code == 0, green_output
        assert "DRIFT" not in green_output
        assert "tempo_traces" in green_output and "[OK]" in green_output

        # 2. Hand-edit Tempo's retention to a WRONG window (the
        # pre-ratification 24h default) -> RED.
        (obs_stack_dir / "tempo" / "tempo.yml").write_text(
            yaml.safe_dump({"compactor": {"compaction": {"block_retention": "24h"}}})
        )
        exit_code = mod.run_drift_check(config_path, obs_stack_dir)
        red_output = capsys.readouterr().out
        assert exit_code == 1, red_output
        assert "tempo_traces" in red_output
        assert "DRIFT" in red_output
        assert "retention drift: 1 signal(s)" in red_output

        # 3. Restore the correct window -> GREEN again.
        (obs_stack_dir / "tempo" / "tempo.yml").write_text(
            yaml.safe_dump({"compactor": {"compaction": {"block_retention": "7d"}}})
        )
        exit_code = mod.run_drift_check(config_path, obs_stack_dir)
        restored_output = capsys.readouterr().out
        assert exit_code == 0, restored_output
        assert "DRIFT" not in restored_output

    def test_prometheus_flag_drift_then_restored(
        self, mod, tmp_path: Path, capsys
    ) -> None:
        """Same RED->GREEN proof for the compose-flag source kind (not just
        the YAML-pointer kind) — a second, independent parser path."""
        config_path = tmp_path / "retention-windows.yaml"
        _write_ssot(config_path)
        obs_stack_dir = tmp_path / "observability-stack"
        _write_reference_obs_stack(obs_stack_dir)

        assert mod.run_drift_check(config_path, obs_stack_dir) == 0
        capsys.readouterr()

        (obs_stack_dir / "docker-compose.yml").write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "prometheus": {
                            "command": ["--storage.tsdb.retention.time=15d"],
                        },
                    },
                }
            )
        )
        exit_code = mod.run_drift_check(config_path, obs_stack_dir)
        red_output = capsys.readouterr().out
        assert exit_code == 1
        assert "prometheus_metrics" in red_output
        assert "DRIFT" in red_output

        (obs_stack_dir / "docker-compose.yml").write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "prometheus": {
                            "command": ["--storage.tsdb.retention.time=30d"],
                        },
                    },
                }
            )
        )
        assert mod.run_drift_check(config_path, obs_stack_dir) == 0

    def test_real_ssot_against_pre_ratification_defaults_is_drift(
        self, mod, tmp_path: Path, capsys
    ) -> None:
        """Sanity: the REAL, shipped SSOT (retention-windows.yaml) checked
        against a fixture obs-stack seeded with the pre-ratification
        upstream defaults (24h/no-compactor/15d — the documented
        "Context — retention is ACCIDENTAL today" numbers) must report
        DRIFT on all three live-checkable signals. Pins that the shipped
        config is not accidentally a no-op."""
        obs_stack_dir = tmp_path / "observability-stack"
        tempo_dir = obs_stack_dir / "tempo"
        tempo_dir.mkdir(parents=True)
        (tempo_dir / "tempo.yml").write_text(
            yaml.safe_dump({"compactor": {"compaction": {"block_retention": "24h"}}})
        )
        loki_dir = obs_stack_dir / "loki"
        loki_dir.mkdir(parents=True)
        (loki_dir / "loki-config.yml").write_text(
            yaml.safe_dump({"limits_config": {"reject_old_samples_max_age": "168h"}})
        )
        (obs_stack_dir / "docker-compose.yml").write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "prometheus": {
                            "command": ["--storage.tsdb.retention.time=15d"],
                        },
                    },
                }
            )
        )

        exit_code = mod.run_drift_check(_SSOT_CONFIG_PATH, obs_stack_dir)
        output = capsys.readouterr().out
        assert exit_code == 1
        for signal in ("tempo_traces", "loki_logs", "prometheus_metrics"):
            assert signal in output
        # 3 per-row "[DRIFT]" markers — the summary line also contains the
        # word "DRIFT" once (see the assertion message it prints), so this
        # counts the bracketed row marker specifically, not the bare word.
        assert output.count("[DRIFT]") == 3


class TestMain:
    def test_main_exit_zero_default_args(
        self, mod, tmp_path: Path, monkeypatch
    ) -> None:
        config_path = tmp_path / "retention-windows.yaml"
        _write_ssot(config_path)
        absent = tmp_path / "absent-obs-stack"
        exit_code = mod.main(
            ["--config", str(config_path), "--obs-stack-dir", str(absent)]
        )
        assert exit_code == 0

    def test_main_returns_one_on_missing_config(self, mod, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.yaml"
        exit_code = mod.main(
            ["--config", str(missing), "--obs-stack-dir", str(tmp_path / "absent")]
        )
        assert exit_code == 1

    def test_main_returns_one_on_malformed_config_no_traceback(
        self, mod, tmp_path: Path, capsys
    ) -> None:
        """A malformed SSOT config (missing the 'signals' key) must fail
        GRACEFULLY through the full CLI entry point — a clean exit code 1
        with a logged message, never an unhandled traceback reaching the
        caller (a config bug is loud, but not a crash)."""
        malformed = tmp_path / "retention-windows.yaml"
        malformed.write_text("not_signals: true\n")
        exit_code = mod.main(
            ["--config", str(malformed), "--obs-stack-dir", str(tmp_path / "absent")]
        )
        assert exit_code == 1

    def test_main_reads_obs_stack_dir_env_default(
        self, mod, tmp_path: Path, monkeypatch
    ) -> None:
        config_path = tmp_path / "retention-windows.yaml"
        _write_ssot(config_path)
        env_dir = tmp_path / "from-env"
        monkeypatch.setenv("OBS_STACK_DIR", str(env_dir))
        exit_code = mod.main(["--config", str(config_path)])
        assert exit_code == 0


class TestSignalResultRender:
    def test_render_ok_declared(self, mod) -> None:
        result = mod.SignalResult(
            "minio_memory_objects", "no-expiry", None, checked_live=False
        )
        rendered = result.render()
        assert "OK" in rendered
        assert "declared" in rendered

    def test_render_drift_live(self, mod) -> None:
        result = mod.SignalResult("tempo_traces", "7d", "24h", checked_live=True)
        rendered = result.render()
        assert "DRIFT" in rendered
        assert "24h" in rendered
        assert "7d" in rendered
