"""LC-9 TEST-PG-PARITY-18: every Postgres used by tests and CI is the ONE
pinned production-parity image (``scripts/ci/pg_image.py``, PG 18.3).

Guards (each has a neuter, recorded in the LC-9 build record; the scanner's
own behaviour is also pinned by the synthetic-text tests below):

* G1 -- no file under src/, scripts/, tests/, Makefile or .github/ references
  a ``postgres:<tag>`` image other than the pin (docstrings and skip
  messages included, so none can claim a stale version).
* G2 -- the ci.yml ``test`` service image equals the constant (ci.yml cannot
  import it, so this is its single-definition check).
* G3 -- the Makefile resolves ``PG_TEST_IMAGE`` from the constant and the
  ``test-rls-local`` recipe spawns it.
* G4 -- the Python spawn sites consume the constant instead of a literal.
"""

from __future__ import annotations

import inspect
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci.pg_image import PG_TEST_IMAGE, read_pg_test_image
from scripts.neuter import pg as neuter_pg

REPO = Path(__file__).resolve().parent.parent
_SCAN_ROOTS = ("src", "scripts", "tests", ".github", "Makefile")
_SCAN_SUFFIXES = {".py", ".yml", ".yaml", ".sh", ".toml", ".md", ""}

#: ``postgres:<tag>`` image references. A tag starts with a digit
#: (``16``, ``18.3``, ``16-alpine``) or is a named tag. Not matched: DSN hosts
#: (``@postgres:5432``), ports, ``bitnami/postgresql`` and other-namespace
#: images -- the lookbehind rejects a preceding word char, ``@``, ``/``, ``.``
#: or ``-``.
_IMAGE_RX = re.compile(
    r"(?<![\w@/.\-])postgres:"
    r"(?P<tag>(?:\d+(?:\.\d+)*(?:-[\w.]+)?|latest|alpine[\w.\-]*|bookworm[\w.\-]*))"
    r"(?![\w.\-]*:)"
)


def _is_port(tag: str) -> bool:
    return tag.isdigit() and len(tag) >= 4


def find_foreign_images(text: str, pin: str) -> list[str]:
    """Every ``postgres:<tag>`` image reference in ``text`` that is not ``pin``."""
    out: list[str] = []
    for m in _IMAGE_RX.finditer(text):
        tag = m.group("tag")
        if _is_port(tag):
            continue
        ref = f"postgres:{tag}"
        if ref != pin:
            out.append(ref)
    return out


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        p = REPO / root
        if p.is_file():
            files.append(p)
            continue
        for f in p.rglob("*"):
            if (
                f.is_file()
                and "__pycache__" not in f.parts
                and f.suffix in _SCAN_SUFFIXES
                and f.name != Path(__file__).name
            ):
                files.append(f)
    return files


def test_pin_is_postgres_18_3() -> None:
    assert PG_TEST_IMAGE == "postgres:18.3"


def test_no_executable_site_references_another_postgres_image() -> None:
    files = _scanned_files()
    # positive control: the scan reaches the sites that must carry the pin
    names = {f.relative_to(REPO).as_posix() for f in files}
    assert {"Makefile", ".github/workflows/ci.yml", "tests/_pg_ephemeral.py"} <= names
    offenders = {
        f.relative_to(REPO).as_posix(): bad
        for f in files
        if (bad := find_foreign_images(f.read_text(errors="replace"), PG_TEST_IMAGE))
    }
    assert offenders == {}


def test_scanner_flags_the_old_pin_a_second_pin_and_loose_tags() -> None:
    pin = PG_TEST_IMAGE
    old = "postgres:" + "16"
    assert find_foreign_images(f"docker run {old} -c x", pin) == [old]
    assert find_foreign_images("image: postgres:18.2", pin) == ["postgres:18.2"]
    assert find_foreign_images("image: postgres:18", pin) == ["postgres:18"]
    assert find_foreign_images("FROM postgres:" + "16-alpine", pin) == [
        "postgres:16-alpine"
    ]
    assert find_foreign_images("image: postgres:latest", pin) == ["postgres:latest"]
    assert len(find_foreign_images(f"{old}\n{old}", pin)) == 2


def test_scanner_ignores_the_pin_dsns_ports_and_other_namespaces() -> None:
    pin = PG_TEST_IMAGE
    ok = (
        f"docker run {pin} -c fsync=off\n"
        "postgresql+asyncpg://u:p@postgres:5432/db\n"
        "postgres:15432/audittrace\n"
        "postgresql://postgres:x@127.0.0.1:1/nosuchdb\n"
        "bitnami/postgresql:16\n"
        "ghcr.io/lfdesousa/audittrace-postgresql:18.3-bitnami-frozen-apr17\n"
    )
    assert find_foreign_images(ok, pin) == []


def test_ci_service_image_equals_the_constant() -> None:
    wf = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    assert wf["jobs"]["test"]["services"]["postgres"]["image"] == PG_TEST_IMAGE


def test_makefile_resolves_the_constant_and_rls_target_spawns_it() -> None:
    out = subprocess.run(
        ["make", "-n", "test-rls-local"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    assert f"{PG_TEST_IMAGE} -c fsync=off" in out


def test_python_spawn_sites_consume_the_constant() -> None:
    assert inspect.signature(neuter_pg.start_container).parameters["tag"].default == (
        PG_TEST_IMAGE
    )
    src = (REPO / "tests/_pg_ephemeral.py").read_text()
    assert "PG_TEST_IMAGE," in src
    assert 'postgres:"' not in src


def test_mk_fragment_is_the_single_definition_and_parser_fails_closed() -> None:
    mk = (REPO / "scripts/ci/pg_image.mk").read_text()
    assert read_pg_test_image(mk) == PG_TEST_IMAGE
    assert read_pg_test_image("PG_TEST_IMAGE := postgres:99\n") == "postgres:99"
    for bad in ("", "# nothing\n", "PG_TEST_IMAGE = a\nPG_TEST_IMAGE = b\n"):
        with pytest.raises(ValueError, match="exactly one PG_TEST_IMAGE"):
            read_pg_test_image(bad)


def test_makefile_resolves_the_pin_with_a_stripped_path() -> None:
    """Regression for the CI-shaped gate: tests/test_ci_make_targets_resolve_tools
    run make with a PATH that has no sed/awk, so the pin must resolve with make
    builtins only (``include``), never a ``$(shell ...)`` helper."""
    assert "include scripts/ci/pg_image.mk" in (REPO / "Makefile").read_text()
    make = shutil.which("make")
    assert make is not None
    out = subprocess.run(
        [make, "-n", "test-rls-local"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env={"PATH": str(Path(make).parent)},
    ).stdout
    assert f"{PG_TEST_IMAGE} -c fsync=off" in out
