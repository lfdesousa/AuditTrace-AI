"""LC-5 / M3-WU-4 -- the five tracked ``scripts/systemd/*.service`` units.

Parsed statically (there is no systemd in CI) and independently of the apply
script's shell parser. Boot-race properties (spec ADDENDUM 2 R1'(c)):

* ``Wants=k3s.service`` AND ``After=k3s.service`` (never ``Requires=``: a
  transient k3s first-boot failure would cancel the dependent's start job
  permanently -- DEPLOYMENT section 9);
* ``Restart=on-failure`` with ``RestartSec`` >= 3 s;
* ``StartLimitBurst=5`` / ``StartLimitIntervalSec=10s`` pinned explicitly;
* ``ExecStart=`` is exactly the launcher (the flags live in the launcher, pinned
  by tests/test_llama_lock_launchers.py).

Every property has a tmp-text neuter that turns exactly that check RED.
"""

from __future__ import annotations

import re

import pytest

from tests._llama_sandbox import DORMANT, SCRIPTS_DIR, UNITS

UNIT_DIR = SCRIPTS_DIR / "systemd"


def parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """{section: {Directive: [values...]}} -- comments skipped, backslash
    continuations joined."""
    out: dict[str, dict[str, list[str]]] = {}
    section = ""
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith(("#", ";"))):
            continue
        if pending:
            line = pending + " " + line
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1].rstrip()
            continue
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            section = m.group(1)
            out.setdefault(section, {})
            continue
        key, _, value = line.partition("=")
        out.setdefault(section, {}).setdefault(key, []).append(value)
    return out


def _seconds(value: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|min)?", value.strip())
    assert m, f"unparseable systemd time {value!r}"
    n, unit = float(m.group(1)), m.group(2) or "s"
    return n * {"ms": 0.001, "s": 1.0, "min": 60.0}[unit]


def _words(unit: dict, section: str, key: str) -> set[str]:
    return {w for v in unit.get(section, {}).get(key, []) for w in v.split()}


def assert_boot_and_restart_pins(unit: dict) -> None:
    assert "k3s.service" in _words(unit, "Unit", "Wants"), (
        "Wants= must carry k3s.service"
    )
    assert "k3s.service" in _words(unit, "Unit", "After"), (
        "After= must carry k3s.service"
    )
    for key in ("Requires", "BindsTo", "PartOf", "Requisite"):
        assert "k3s.service" not in _words(unit, "Unit", key), (
            f"{key}=k3s.service would cancel this unit's start for good on a k3s first-boot failure"
        )
    assert unit["Service"].get("Restart") == ["on-failure"]
    rs = unit["Service"].get("RestartSec")
    assert rs and _seconds(rs[0]) >= 3, "RestartSec must be >= 3s"
    assert unit["Unit"].get("StartLimitBurst") == ["5"]
    assert unit["Unit"].get("StartLimitIntervalSec") == ["10s"]


def assert_execstart_is_the_launcher(unit: dict, launcher: str) -> None:
    assert unit["Service"].get("ExecStart") == [f"@SCRIPTS_DIR@/{launcher}"], (
        "ExecStart= must be exactly the launcher: no inline flags, no second ExecStart"
    )


def assert_key_file_env(unit: dict) -> None:
    assert "LLAMA_API_KEY_FILE=@KEY_FILE@" in [
        v.strip('"') for v in unit["Service"].get("Environment", [])
    ], "the key file path must come from the single @KEY_FILE@ substitution"


@pytest.fixture(params=sorted(UNITS))
def unit_name(request) -> str:
    return request.param


def _load(name: str, text: str | None = None) -> dict:
    return parse_unit(
        text if text is not None else (UNIT_DIR / f"{name}.service").read_text()
    )


class TestUnits:
    def test_boot_order_and_restart_pins(self, unit_name):
        assert_boot_and_restart_pins(_load(unit_name))

    def test_execstart_is_exactly_the_launcher(self, unit_name):
        assert_execstart_is_the_launcher(_load(unit_name), UNITS[unit_name][0])

    def test_key_file_comes_from_the_single_substitution(self, unit_name):
        assert_key_file_env(_load(unit_name))

    def test_every_launcher_exists_and_is_executable(self, unit_name):
        launcher = SCRIPTS_DIR / UNITS[unit_name][0]
        assert launcher.is_file() and launcher.stat().st_mode & 0o111

    def test_no_literal_key_bind_or_flags_in_the_unit(self, unit_name):
        lines = [
            ln
            for ln in (UNIT_DIR / f"{unit_name}.service").read_text().splitlines()
            if not ln.lstrip().startswith("#")
        ]
        text = "\n".join(lines)
        assert (
            "--api-key" not in text and "--host" not in text and "0.0.0.0" not in text
        )

    def test_units_are_enabled_by_the_same_target_as_before(self, unit_name):
        assert _load(unit_name)["Install"].get("WantedBy") == ["multi-user.target"]

    def test_dormant_units_are_defined_but_the_script_never_enables_them(
        self, unit_name
    ):
        if unit_name in DORMANT:
            assert "DORMANT" in (UNIT_DIR / f"{unit_name}.service").read_text()


# ─────────────── neuters: each property turns exactly its check RED ───────────────

_UNIT = "llama-server"


def _mutated(old: str, new: str) -> dict:
    text = (UNIT_DIR / f"{_UNIT}.service").read_text()
    assert old in text, f"neuter anchor {old!r} drifted from the unit"
    mutated = text.replace(old, new)
    assert mutated != text
    return parse_unit(mutated)


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("drop_wants", "Wants=k3s.service", "Wants="),
        (
            "drop_after",
            "After=network.target systemd-udev-settle.service k3s.service",
            "After=network.target systemd-udev-settle.service",
        ),
        ("requires_instead_of_wants", "Wants=k3s.service", "Requires=k3s.service"),
        ("restart_sec_too_short", "RestartSec=5s", "RestartSec=1s"),
        ("restart_policy", "Restart=on-failure", "Restart=always"),
        ("burst", "StartLimitBurst=5", "StartLimitBurst=0"),
        ("interval", "StartLimitIntervalSec=10s", "StartLimitIntervalSec=0"),
    ],
)
def test_neutering_a_pin_turns_the_pin_check_red(label, old, new):
    with pytest.raises(AssertionError):
        assert_boot_and_restart_pins(_mutated(old, new))


def test_neutering_execstart_to_inline_flags_turns_the_launcher_check_red():
    unit = _mutated(
        "ExecStart=@SCRIPTS_DIR@/start-chat-llama.sh",
        "ExecStart=/usr/bin/llama-server --host 0.0.0.0 --port 11435",
    )
    with pytest.raises(AssertionError):
        assert_execstart_is_the_launcher(unit, "start-chat-llama.sh")


def test_neutering_the_key_file_env_turns_its_check_red():
    unit = _mutated('Environment="LLAMA_API_KEY_FILE=@KEY_FILE@"', "")
    with pytest.raises(AssertionError):
        assert_key_file_env(unit)


def test_the_parser_reads_continuations_and_comments():
    parsed = parse_unit(
        "[Service]\n# c\nExecStart=/a \\\n  --b \\\n  --c\nRestart=on-failure\n"
    )
    assert parsed["Service"]["ExecStart"] == ["/a --b --c"]
    assert parsed["Service"]["Restart"] == ["on-failure"]


def test_the_time_parser_rejects_garbage():
    with pytest.raises(AssertionError):
        _seconds("soon")
    assert _seconds("2min") == 120 and _seconds("500ms") == 0.5
