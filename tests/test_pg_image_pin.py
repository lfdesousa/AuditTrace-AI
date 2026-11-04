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

import ast
import inspect
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci.pg_image import PG_TEST_IMAGE, read_pg_test_image
from scripts.neuter import pg as neuter_pg
from tests._hermetic_env import hermetic_env

REPO = Path(__file__).resolve().parent.parent
_SCAN_ROOTS = ("src", "scripts", "tests", ".github", "Makefile")
_SCAN_SUFFIXES = {".py", ".yml", ".yaml", ".sh", ".toml", ".md", ".json", ""}
#: Compose files and Dockerfiles (any suffix, any depth) are scanned wherever
#: they live, e.g. the root ``docker-compose*.yml`` (e2e.yml runs them) and
#: ``tests/Dockerfile.pg``.
_IMAGE_FILE_RX = re.compile(r"^(Dockerfile.*|(docker-)?compose.*\.ya?ml)$")
_PRUNE_DIRS = {
    ".git",
    ".venv",
    ".gate-venv",
    "node_modules",
    "evidence",
    "docs",
    "charts",
    "htmlcov",
    "__pycache__",
}

#: Registry hosts that Docker (or the same hub) serve the official image
#: from. Matched case-insensitively: a spelling variant must not be a way
#: round the guard.
_REGISTRY_RX = re.compile(
    r"^(?:index\.docker\.io|registry-1\.docker\.io|docker\.io)/", re.IGNORECASE
)
#: Leading ``./`` / ``../`` segments of a relative path (``./postgres:16``): the
#: repository is still the official image, so they are stripped before comparison.
_RELATIVE_RX = re.compile(r"^(?:\.{1,2}/)+")
#: A candidate image token (DENY-BY-DEFAULT, r4/r6): an optional path of
#: segments, then a repository whose LAST segment is ``postgres`` (any case),
#: then ANY Docker-grammar tag ``:[\w][\w.\-]{0,127}`` and/or ANY ``@sha256:``
#: digest. No tag family is enumerated: whether the reference is acceptable is
#: decided only by equality with the pin in :func:`find_foreign_images`. The
#: tag is possessive so the engine cannot backtrack to a shorter tag to dodge
#: the lookahead. ``(?![\w.\-]*:)`` keeps ``postgres:5432:5432``-style
#: host:port strings out.
#: RULING (r6, flipped): an ``@`` after ``postgres:<tag>`` NEVER excludes the
#: reference, whatever follows it (it is captured as ``at`` and kept on the
#: reference, so it can never equal the pin).
#: RULING (r7, allowlist): the ONLY exclusion is a DSN, decided in
#: :func:`find_foreign_images` by :data:`_DSN_PREFIX_RX` on the text before the
#: match. The lookbehind refuses only a match that starts after a word char,
#: ``@`` (a DSN host) or ``.``/``-``; it no longer excludes ``/``, so a match
#: after ``docker://``, ``docker-image://``, ``oci://``, ``/``, ``//`` or ``./``
#: is a candidate. Whether the repository IS the official image is decided by
#: :func:`normalise_repo`, not by this pattern.
_IMAGE_RX = re.compile(
    r"(?<![\w@.\-])(?P<repo>(?:[\w.\-]+/)*postgres)"
    r"(?:"
    r":(?P<tag>\w[\w.\-]{0,127}+)(?:@(?P<tagdigest>sha256:\w+))?"
    r"(?![\w.\-]*:)(?P<at>@)?"
    r"|@(?P<digest>sha256:\w+)"
    r")",
    re.IGNORECASE,
)
#: DSN allowlist (r7, case-insensitive): the text immediately before a match
#: must END with ``postgres://``, ``postgresql://`` or ``postgresql+<driver>://``.
#: Any other prefix (``docker://``, ``docker-image://``, ``oci://``, ``/``,
#: ``//``, ``./``, a path) leaves the match a RED candidate.
_DSN_PREFIX_RX = re.compile(r"postgres(?:ql(?:\+\w+)?)?://\Z", re.IGNORECASE)
#: An untagged (implicit ``:latest``) image, only where it IS an image
#: reference: a compose/workflow ``image:`` value or a Dockerfile ``FROM``.
#: The bare word "postgres" elsewhere (users, hosts, prose) is deliberately not
#: matched -- see :func:`_bare_in_docker_argv` for Python argv.
_BARE_IMAGE_RX = re.compile(
    r"(?im)^\s*(?:image:|FROM(?:\s+--\S+)?)\s*(?:&\w+\s+)?[\"']?"
    r"(?P<repo>(?:[\w.\-]+/)*postgres)[\"']?(?:\s+AS\s+\S+)?\s*(?:#.*)?$"
)
#: ``${VAR:-x}``, ``${VAR-x}``, ``${VAR:=x}``, ``${VAR=x}``, ``${VAR:?x}`` and
#: ``${VAR?x}``: innermost first (the value class excludes braces).
_PARAM_DEFAULT_RX = re.compile(r"\$\{[A-Za-z_]\w*:?[-=?+]?([^{}]*)\}")
_PARAM_OPERATOR_RX = re.compile(r"\$\{[A-Za-z_]\w*(:?[-=?])([^{}]*)\}")


def expand_param_defaults(text: str) -> str:
    """Replace shell/compose parameter expansions that carry a default with
    that default, so ``image: ${PG_IMAGE:-postgres:16}`` is scanned as
    ``image: postgres:16`` (nested expansions resolve innermost first). A
    purely dynamic ``${TAG}`` with no default is left alone: what it resolves
    to is not in the file."""
    while True:
        new = _PARAM_OPERATOR_RX.sub(lambda m: m.group(2), text)
        if new == text:
            return text
        text = new


def normalise_repo(repo: str) -> str:
    """``index.docker.io/library/postgres`` -> ``postgres`` (lower-cased; leading
    ``./`` or ``../`` stripped); any other
    namespace (``bitnami/postgres``, ``team/postgres``, ``ghcr.io/x/postgres``)
    is returned unchanged, so it never equals the official ``postgres``."""
    repo = _REGISTRY_RX.sub("", _RELATIVE_RX.sub("", repo)).lower()
    return repo[len("library/") :] if repo.startswith("library/") else repo


def _is_port(tag: str) -> bool:
    return tag.isdigit() and len(tag) >= 4


def _canonical(ref: str) -> str:
    """Normalised form of a whole ``repo[:tag|@digest]`` reference."""
    m = re.match(r"^(?P<repo>[^:@]*)(?P<rest>.*)$", ref)
    assert m is not None  # the pattern matches any string
    return normalise_repo(m.group("repo")) + m.group("rest")


def _bare_in_docker_argv(text: str) -> list[str]:
    """Untagged ``"postgres"`` as an element of a Python ``["docker", ...]``
    argv literal (the spawn position). A literal ``"postgres"`` in any other
    list (a user name) is ignored."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List | ast.Tuple):
            continue
        items = [
            e.value
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if items[:1] == ["docker"] and any(
            normalise_repo(i) == "postgres" for i in items[1:]
        ):
            out.append("postgres")
    return out


def find_foreign_images(text: str, pin: str) -> list[str]:
    """Every reference to the OFFICIAL Postgres image in ``text`` that is not
    ``pin``. Parameter defaults are expanded first, every candidate token's
    repository is normalised, and only repositories that normalise to exactly
    ``postgres`` count."""
    text = expand_param_defaults(text)
    out: list[str] = []
    for m in _IMAGE_RX.finditer(text):
        if normalise_repo(m.group("repo")) != "postgres":
            continue
        if _DSN_PREFIX_RX.search(text[max(0, m.start() - 128) : m.start()]):
            continue  # r7 allowlist: a postgres-family DSN, never an image
        tag = m.group("tag")
        if tag is not None and _is_port(tag):
            continue
        # a trailing dot is sentence punctuation, never part of a real tag
        ref = "postgres" + (f":{tag.rstrip('.')}" if tag else "")
        digest = m.group("tagdigest") or m.group("digest")
        if digest:
            # RULING (r4): the pin carries no digest policy, so ANY digest
            # form -- bare or behind a tag, the pinned tag included -- is not
            # the pin and is RED.
            ref += "@" + digest
        if m.group("at"):
            # RULING (r6): an ``@`` after the tag is never an exclusion (only a
            # ``scheme://`` DSN is, via the lookbehind); the reference keeps
            # the ``@`` so it is never the pin, whatever follows it.
            ref += "@"
        if ref != pin:
            out.append(ref)
    out.extend(
        "postgres"
        for m in _BARE_IMAGE_RX.finditer(text)
        if normalise_repo(m.group("repo")) == "postgres"
    )
    out.extend(_bare_in_docker_argv(text))
    return out


def _scanned_files() -> list[Path]:
    files: dict[Path, None] = {}
    for root in _SCAN_ROOTS:
        p = REPO / root
        if p.is_file():
            files[p] = None
            continue
        for f in p.rglob("*"):
            if (
                f.is_file()
                and "__pycache__" not in f.parts
                and f.suffix in _SCAN_SUFFIXES
            ):
                files[f] = None
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for name in filenames:
            if _IMAGE_FILE_RX.match(name):
                files[Path(dirpath) / name] = None
    return [f for f in files if f.name != Path(__file__).name]


def test_pin_is_postgres_18_3() -> None:
    assert PG_TEST_IMAGE == "postgres:18.3"


def test_no_executable_site_references_another_postgres_image() -> None:
    files = _scanned_files()
    # positive control: the scan reaches the sites that must carry the pin
    names = {f.relative_to(REPO).as_posix() for f in files}
    assert {
        "Makefile",
        ".github/workflows/ci.yml",
        "tests/_pg_ephemeral.py",
        "docker-compose.yml",
        "docker-compose.edge.yml",
        "Dockerfile",
        "Dockerfile.tests",
    } <= names
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


def test_makefile_resolves_the_pin_with_a_stripped_path(tmp_path: Path) -> None:
    """Regression for the CI-shaped gate: tests/test_ci_make_targets_resolve_tools
    run make with a PATH that has no sed/awk, so the pin must resolve with make
    builtins only (``include``), never a ``$(shell ...)`` helper."""
    assert "include scripts/ci/pg_image.mk" in (REPO / "Makefile").read_text()
    make = shutil.which("make")
    assert make is not None
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "make").symlink_to(make)
    out = subprocess.run(  # noqa: S603 - fixed argv
        [str(shim / "make"), "-n", "test-rls-local"],
        cwd=REPO,
        env=hermetic_env({"PATH": str(shim)}),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    assert f"{PG_TEST_IMAGE} -c fsync=off" in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("docker.io/library/postgres:" + "16", ["postgres:16"]),
        ("library/postgres:" + "16", ["postgres:16"]),
        ("docker.io/postgres:" + "16", ["postgres:16"]),
        ("postgres@sha256:" + "ab12" * 16, ["postgres@sha256:" + "ab12" * 16]),
        ("docker.io/library/postgres@sha256:" + "ab12" * 16, None),
        ("image: postgres", ["postgres"]),
        ("    image: 'docker.io/library/postgres'  # dev", ["postgres"]),
        ("FROM postgres", ["postgres"]),
        ("FROM --platform=linux/amd64 postgres AS db", ["postgres"]),
        ('x = ["docker", "run", "--rm", "postgres"]', ["postgres"]),
        ("index.docker.io/library/postgres:" + "16", ["postgres:16"]),
        ("registry-1.docker.io/library/postgres:" + "16", ["postgres:16"]),
        ("DOCKER.IO/library/postgres:" + "16", ["postgres:16"]),
        ("Docker.Io/Library/postgres:" + "16", ["postgres:16"]),
        ("image: ${PG_IMAGE:-postgres:" + "16}", ["postgres:16"]),
        ("image: ${PG_IMAGE-postgres:" + "16}", ["postgres:16"]),
        ("image: ${PG_IMAGE:=postgres:" + "16}", ["postgres:16"]),
        ("image: ${PG_IMAGE=postgres:" + "16}", ["postgres:16"]),
        ("image: ${PG_IMAGE:?postgres:" + "16}", ["postgres:16"]),
        (
            "image: ${A:-${B:-index.docker.io/library/postgres:" + "16}}",
            ["postgres:16"],
        ),
        ("image: &pg postgres:" + "16", ["postgres:16"]),
        ("image: ${PG_IMAGE:-postgres}", ["postgres"]),
        # r4 deny-by-default: ANY tag / digest that is not the pin is RED
        ("image: postgres:trixie", ["postgres:trixie"]),
        ("image: postgres:bullseye", ["postgres:bullseye"]),
        ("image: docker.io/library/postgres:bookworm", ["postgres:bookworm"]),
        ("image: postgres:18-trixie", ["postgres:18-trixie"]),
        ("image: POSTGRES:" + "16", ["postgres:16"]),
        ("image: postgres:some_new.tag-9", ["postgres:some_new.tag-9"]),
        ("image: postgres:" + "x" * 128, ["postgres:" + "x" * 128]),
        ("see postgres:" + "16.", ["postgres:16"]),
        ("image: " + PG_TEST_IMAGE + "-x", [PG_TEST_IMAGE + "-x"]),
        (
            "image: ${PG_IMAGE:-docker.io/library/postgres:trixie}",
            ["postgres:trixie"],
        ),
        # RULING: the pin has no digest policy, so a digest on the pinned tag
        # (any registry spelling) is NOT the pin and is RED.
        (
            PG_TEST_IMAGE + "@sha256:" + "cd34" * 16,
            [f"{PG_TEST_IMAGE}@sha256:" + "cd34" * 16],
        ),
        (
            "docker.io/library/" + PG_TEST_IMAGE + "@sha256:" + "cd34" * 16,
            [f"{PG_TEST_IMAGE}@sha256:" + "cd34" * 16],
        ),
        (
            "postgres:trixie@sha256:" + "cd34" * 16,
            ["postgres:trixie@sha256:" + "cd34" * 16],
        ),
        ("postgres@sha256:" + "CD34" * 16, ["postgres@sha256:" + "CD34" * 16]),
        # r5: a literal tag followed by a non-credential ``@`` is RED, never
        # a credential: env-supplied digest, other algorithm, empty digest
        ("postgres:16@${PG_DIGEST}", ["postgres:16@"]),
        ("image: postgres:16@sha256:${PG_DIGEST}", ["postgres:16@"]),
        ("postgres:16@sha512:" + "ab" * 64, ["postgres:16@"]),
        ("image: postgres:16@", ["postgres:16@"]),
        ("image: 'postgres:16@'", ["postgres:16@"]),
        ("image: " + PG_TEST_IMAGE + "@${PG_DIGEST}", [PG_TEST_IMAGE + "@"]),
        ("image: " + PG_TEST_IMAGE + "@sha512:" + "ab" * 64, [PG_TEST_IMAGE + "@"]),
        ("image: " + PG_TEST_IMAGE + "@", [PG_TEST_IMAGE + "@"]),
        ("image: postgres:16@SHA256:${PG_DIGEST}", ["postgres:16@"]),
        # r6 (flipped rule): after ``postgres:<tag>@`` the reference is ALWAYS
        # a candidate, whatever follows -- unbraced/paren/escaped expansions,
        # other algorithms, template and format tails, credentials.
        ("postgres:16@$PG_DIGEST", ["postgres:16@"]),
        ("postgres:16@$(PG_DIGEST)", ["postgres:16@"]),
        ("postgres:16@$${PG_DIGEST}", ["postgres:16@"]),
        ("postgres:16@sha256:$PG_DIGEST", ["postgres:16@"]),
        ("postgres:16@sha256:$(PG_DIGEST)", ["postgres:16@"]),
        ("postgres:16@sha384:" + "ab" * 48, ["postgres:16@"]),
        ("postgres:16@{{ d }}", ["postgres:16@"]),
        ("postgres:16@%s", ["postgres:16@"]),
        ("postgres:pw@$HOST", ["postgres:pw@"]),
        ("postgres:pw@${HOST}", ["postgres:pw@"]),
        # DECISION (operator, 2026-11-04, LC-9 r6 flipped rule): a bare
        # ``postgres:secret@db.internal`` (no ``scheme://``) is now RED.
        ("postgres:secret@db.internal", ["postgres:secret@"]),
        ("postgres:secret@host:5432/audit", ["postgres:secret@"]),
        # the ONLY exclusion: a DSN (``://`` precedes it), whatever the tail
        ("postgres://postgres:pw@${HOST}/db", []),
        ("postgres://u:p@$HOST/db", []),
        ("postgresql://postgres:pw@$(HOST)/db", []),
        ("postgresql+asyncpg://postgres:pw@${HOST}:5432/db", []),
        ("postgresql+asyncpg://postgres:secret@postgres:5432", []),
        ("POSTGRESQL://postgres:pw@${HOST}/db", []),
        ("PostgreSQL+AsyncPG://u:p@$HOST/db", []),
        # r7 allowlist: only a postgres-family scheme excludes; every other
        # prefix (transport schemes, bare slashes, paths) stays RED
        ("skopeo inspect docker://postgres:16", ["postgres:16"]),
        (
            "skopeo copy docker://docker.io/library/postgres:16 oci:x",
            ["postgres:16"],
        ),
        (
            "docker buildx build --build-context pg=docker-image://postgres:16 .",
            ["postgres:16"],
        ),
        ("FROM docker-image://postgres:16", ["postgres:16"]),
        ("podman pull docker://postgres:16@$D", ["postgres:16@"]),
        ("oci://postgres:16", ["postgres:16"]),
        ("/postgres:16", ["postgres:16"]),
        ("//postgres:16", ["postgres:16"]),
        ("./postgres:16", ["postgres:16"]),
        ("../postgres:16", ["postgres:16"]),
        ("docker://postgres://postgres:16", []),  # prefix ends in a postgres DSN
        ("xpostgresql+://postgres:16", ["postgres:16"]),  # empty driver is not one
        # the pin in any canonical spelling is fine
        ("docker.io/library/" + PG_TEST_IMAGE, []),
        ("library/" + PG_TEST_IMAGE, []),
        ("image: ${PG_IMAGE:-" + PG_TEST_IMAGE + "}", []),
        ("index.docker.io/library/" + PG_TEST_IMAGE, []),
        ("image: postgres:${PG_TAG}", []),  # purely dynamic: out of contract
    ],
)
def test_scanner_catches_every_spelling_of_the_official_image(
    text: str, expected: list[str] | None
) -> None:
    found = find_foreign_images(text, PG_TEST_IMAGE)
    if expected is None:  # digest behind the canonical prefix: one hit, digest form
        assert len(found) == 1
        assert found[0].startswith("postgres@sha256:")
    else:
        assert found == expected


def test_scanner_has_no_false_positives_on_prose_users_hosts_or_urls() -> None:
    ok = (
        "The postgres user owns it; connect as postgres to the postgres host.\n"
        "POSTGRES_USER=postgres\n"
        "psql -U postgres -d audit\n"
        "see https://www.postgresql.org/docs/ and https://hub.docker.com/_/postgres\n"
        "image: ghcr.io/lfdesousa/audittrace-postgresql:18.3-bitnami-frozen-apr17\n"
        "image: bitnami/postgresql:" + "16\n"
        "image: ${PG_IMAGE:-ghcr.io/lfdesousa/audittrace-postgresql:18.3}\n"
        "image: ${PG_IMAGE:-bitnami/postgres:" + "16}\n"
        "image: localhost:5000/postgres:" + "16\n"
        "host: ${DB_HOST:-postgres}\n"
        "url=postgresql://u:${PW:-x}@postgres:5432/db\n"
        "image: registry.example.com/team/postgres:" + "16\n"
        'USERS = ["postgres", "audittrace"]\n'
        'argv = ["psql", "-U", "postgres"]\n'
        "postgres:5432\n"
        "ports: postgres:5432:5432\n"
        "postgresql://u:p@host:5432/db\n"
        "postgresql+asyncpg://postgres:secret@postgres:5432/audit\n"
        "postgres://postgres:pw@${HOST}/audit\n"
        "postgres_exporter --web.listen-address=:9187\n"
        "image: quay.io/prometheuscommunity/postgres-exporter:v0.15.0\n"
        "docker.io/library/" + PG_TEST_IMAGE + "\n"
        "POSTGRES_PASSWORD: postgres\n"
    )
    assert find_foreign_images(ok, PG_TEST_IMAGE) == []


def test_compose_and_dockerfiles_are_scanned() -> None:
    names = {f.relative_to(REPO).as_posix() for f in _scanned_files()}
    assert "docker-compose.yml" in names  # positive control (F2)
    assert {"docker-compose.edge.yml", "Dockerfile", "bff/Dockerfile"} <= names
    assert _IMAGE_FILE_RX.match("Dockerfile.pg")
    assert _IMAGE_FILE_RX.match("docker-compose.test-pg.yml")
    assert _IMAGE_FILE_RX.match("compose.yaml")
    assert not _IMAGE_FILE_RX.match("test_compose_drift.py")
