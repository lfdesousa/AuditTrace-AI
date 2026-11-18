"""Drive the REAL ``.githooks/pre-push`` through a REAL ``git push`` in a
throwaway repo (tmp dir + a bare "remote") and assert exactly which
commit range reaches the secret scanner (PREPUSH-NEW-BRANCH-SCAN-RANGE).

The scanner is a stub ``gitleaks`` script placed first on PATH that records
its arguments, so these tests do not need gitleaks installed. The number of
commits "handed to the scanner" is measured by running ``git rev-list
--count`` on the ``--log-opts`` range the stub recorded, so a hook that
scans ``--staged`` (the pre-fix behaviour: 0 commits) or the wrong range
fails the exact-count assertions.

Guards (each has a test that fails when the guard is removed):

  - new branch       -> scans ``<merge-base with default branch>..<local>``
  - update push      -> scans ``<remote sha>..<local sha>``
  - delete push      -> scans nothing, exits 0
  - scanner exit != 0 -> the hook (and the push) fail
  - range uncomputable (no default branch / unknown remote sha) -> fail closed
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".githooks" / "pre-push"

_STUB = """#!/usr/bin/env bash
# Stub scanner: record argv (one invocation per line, shell-quoted).
printf '%q ' "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
calls="$(wc -l < "$STUB_LOG")"
[ -n "${STUB_FAIL_AT:-}" ] && [ "$calls" -eq "$STUB_FAIL_AT" ] && exit 1
exit "${STUB_EXIT:-0}"
"""


@dataclass
class Rig:
    work: Path
    remote: Path
    log: Path
    env: dict[str, str]

    def git(self, *args: str, cwd: Path | None = None) -> str:
        res = subprocess.run(
            ["git", *args],
            cwd=cwd or self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()

    def commit(self, n: int, tag: str) -> list[str]:
        shas = []
        for i in range(n):
            (self.work / f"{tag}-{i}.txt").write_text(f"{tag} {i}\n")
            self.git("add", ".")
            self.git("commit", "-q", "-m", f"{tag} {i}")
            shas.append(self.git("rev-parse", "HEAD"))
        return shas

    def push(
        self,
        *args: str,
        exit_code: str = "0",
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {**self.env, "STUB_EXIT": exit_code, **(extra_env or {})}
        return subprocess.run(
            ["git", "push", *args],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
        )

    def scanner_calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [shlex.split(line) for line in self.log.read_text().splitlines()]

    def scanned_commit_count(self) -> int:
        """Commits handed to the scanner by the single recorded invocation."""
        calls = self.scanner_calls()
        assert len(calls) == 1, f"expected exactly one scanner call, got {calls}"
        opts = [a for a in calls[0] if a.startswith("--log-opts")]
        assert len(opts) == 1, f"scanner got no commit range: {calls[0]}"
        rng = opts[0].split("=", 1)[1]
        return int(self.git("rev-list", "--count", rng))


def _stub_first_path(stub_dir: Path) -> str:
    """PATH with the stub scanner ahead of everything EXCEPT the first entry:
    the hermetic guard (tests/conftest.py) requires PATH[0] to stay the CI
    shim dir on every child process."""
    first, _, rest = os.environ["PATH"].partition(os.pathsep)
    return os.pathsep.join([first, str(stub_dir), rest])


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "gitleaks"
    stub.write_text(_STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "scanner.log"
    env = {
        **os.environ,
        "PATH": _stub_first_path(stub_dir),
        "STUB_LOG": str(log),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    env.pop("PR_BODY", None)
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True,
        env=env,
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, env=env)
    r = Rig(work=work, remote=remote, log=log, env=env)
    # The hook's PR-body step lives next to it in the real repo; give the
    # throwaway repo the same files so the REAL hook runs end to end.
    (work / "scripts").mkdir()
    shutil.copytree(REPO_ROOT / "scripts" / "hooks", work / "scripts" / "hooks")
    shutil.copy(REPO_ROOT / "scripts" / "__init__.py", work / "scripts" / "__init__.py")
    shutil.copy(
        REPO_ROOT / "scripts" / "adr049_evidence_check.py",
        work / "scripts" / "adr049_evidence_check.py",
    )
    r.git("remote", "add", "origin", str(remote))
    r.commit(1, "base")
    r.git("push", "-q", "origin", "main")  # no hook installed yet
    r.git("remote", "set-head", "origin", "main")
    hook_dst = work / ".git" / "hooks" / "pre-push"
    shutil.copy(HOOK, hook_dst)
    hook_dst.chmod(0o755)
    return r


def test_new_branch_one_commit_scans_exactly_one(rig: Rig) -> None:
    rig.git("checkout", "-q", "-b", "feat")
    rig.commit(1, "f")
    res = rig.push("-u", "origin", "feat")
    assert res.returncode == 0, res.stderr
    assert rig.scanned_commit_count() == 1


def test_new_branch_three_commits_scans_exactly_three(rig: Rig) -> None:
    rig.git("checkout", "-q", "-b", "feat")
    rig.commit(3, "f")
    res = rig.push("-u", "origin", "feat")
    assert res.returncode == 0, res.stderr
    assert rig.scanned_commit_count() == 3


def test_new_branch_excludes_commits_already_on_default(rig: Rig) -> None:
    # main advances on the remote after the base; the new branch forks from
    # the newer main, so only its own commits are scanned.
    rig.commit(2, "main-more")
    rig.git("push", "-q", "--no-verify", "origin", "main")
    rig.git("checkout", "-q", "-b", "feat")
    rig.commit(2, "f")
    res = rig.push("-u", "origin", "feat")
    assert res.returncode == 0, res.stderr
    assert rig.scanned_commit_count() == 2


@pytest.mark.parametrize("n", [1, 2])
def test_update_push_scans_exactly_the_new_commits(rig: Rig, n: int) -> None:
    rig.git("checkout", "-q", "-b", "feat")
    rig.commit(2, "first")
    assert rig.push("-u", "origin", "feat").returncode == 0
    rig.log.unlink()
    rig.commit(n, "more")
    res = rig.push("origin", "feat")
    assert res.returncode == 0, res.stderr
    assert rig.scanned_commit_count() == n


def test_delete_push_scans_nothing_and_exits_zero(rig: Rig) -> None:
    rig.git("checkout", "-q", "-b", "feat")
    rig.commit(1, "f")
    assert rig.push("-u", "origin", "feat").returncode == 0
    rig.log.unlink()
    res = rig.push("origin", "--delete", "feat")
    assert res.returncode == 0, res.stderr
    assert rig.scanner_calls() == []
    assert "feat" not in rig.git("branch", "-r", "--list", "origin/feat")


def test_new_branch_with_no_new_commits_scans_nothing_and_passes(rig: Rig) -> None:
    rig.git("checkout", "-q", "-b", "feat")
    res = rig.push("-u", "origin", "feat")
    assert res.returncode == 0, res.stderr
    assert rig.scanner_calls() == []
    assert rig.git("ls-remote", "--heads", str(rig.remote), "feat") != ""


def test_scanner_nonzero_exit_blocks_the_push(rig: Rig) -> None:
    rig.git("checkout", "-q", "-b", "feat")
    rig.commit(1, "f")
    res = rig.push("-u", "origin", "feat", exit_code="1")
    assert res.returncode != 0
    assert len(rig.scanner_calls()) == 1
    # the remote must not have received the branch
    assert rig.git("ls-remote", "--heads", str(rig.remote), "feat") == ""


def test_new_branch_without_resolvable_default_branch_fails_closed(rig: Rig) -> None:
    rig.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    rig.git("update-ref", "-d", "refs/remotes/origin/main")
    rig.git("checkout", "-q", "-b", "feat")
    rig.commit(1, "f")
    res = rig.push("-u", "origin", "feat")
    assert res.returncode != 0
    assert rig.scanner_calls() == []
    assert rig.git("ls-remote", "--heads", str(rig.remote), "feat") == ""


def test_update_push_with_unknown_remote_sha_fails_closed(
    rig: Rig, tmp_path: Path
) -> None:
    rig.git("checkout", "-q", "-b", "feat")
    rig.commit(1, "mine")
    assert rig.push("-u", "origin", "feat").returncode == 0
    rig.log.unlink()
    # someone else advances the remote branch; this clone never fetches it
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", "-b", "feat", str(rig.remote), str(other)],
        check=True,
        env=rig.env,
    )
    (other / "x.txt").write_text("x\n")
    rig.git("add", ".", cwd=other)
    rig.git("commit", "-q", "-m", "theirs", cwd=other)
    rig.git("push", "-q", "--no-verify", "origin", "feat", cwd=other)
    rig.commit(1, "mine2")
    res = rig.push("--force", "origin", "feat")
    assert res.returncode != 0
    assert rig.scanner_calls() == []


def _range_of(call: list[str]) -> str:
    opts = [a for a in call if a.startswith("--log-opts")]
    assert len(opts) == 1, f"scanner got no commit range: {call}"
    return opts[0].split("=", 1)[1]


def _two_branches(rig: Rig) -> tuple[str, str]:
    """Fork branches a (2 commits) and b (3 commits) from main; return tips."""
    rig.git("checkout", "-q", "-b", "a")
    rig.commit(2, "a")
    tip_a = rig.git("rev-parse", "HEAD")
    rig.git("checkout", "-q", "main")
    rig.git("checkout", "-q", "-b", "b")
    rig.commit(3, "b")
    return tip_a, rig.git("rev-parse", "HEAD")


def test_multi_ref_push_scans_every_ref_exactly(rig: Rig) -> None:
    tip_a, tip_b = _two_branches(rig)
    res = rig.push("origin", "a", "b")
    assert res.returncode == 0, res.stderr
    calls = rig.scanner_calls()
    assert len(calls) == 2, calls
    counts = {}
    for call in calls:
        rng = _range_of(call)
        counts[rng.split("..")[1]] = int(rig.git("rev-list", "--count", rng))
    assert counts == {tip_a: 2, tip_b: 3}


def test_scanner_failure_on_second_ref_blocks_the_whole_push(rig: Rig) -> None:
    _two_branches(rig)
    res = rig.push("origin", "a", "b", extra_env={"STUB_FAIL_AT": "2"})
    assert res.returncode != 0
    assert len(rig.scanner_calls()) == 2  # the second ref WAS scanned
    heads = rig.git("ls-remote", "--heads", str(rig.remote))
    assert "refs/heads/a" not in heads
    assert "refs/heads/b" not in heads


def test_orphan_branch_without_merge_base_fails_closed(rig: Rig) -> None:
    # unrelated history; keep the index (scripts/ stays) so the PR-body step
    # cannot be the reason the push fails
    rig.git("checkout", "-q", "--orphan", "orph")
    (rig.work / "orphan.txt").write_text("unrelated\n")
    rig.git("add", ".")
    rig.git("commit", "-q", "-m", "orphan root")
    res = rig.push("-u", "origin", "orph")
    assert res.returncode != 0
    assert "no merge-base" in res.stderr
    assert rig.scanner_calls() == []
    assert rig.git("ls-remote", "--heads", str(rig.remote), "orph") == ""


def test_default_branch_falls_back_to_main_without_origin_head(rig: Rig) -> None:
    rig.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    rig.git("checkout", "-q", "-b", "feat")
    rig.commit(1, "f")
    res = rig.push("-u", "origin", "feat")
    assert res.returncode == 0, res.stderr
    assert rig.scanned_commit_count() == 1


def test_gitleaks_config_env_reaches_the_scanner(rig: Rig) -> None:
    rig.git("checkout", "-q", "-b", "feat")
    rig.commit(1, "f")
    res = rig.push(
        "-u", "origin", "feat", extra_env={"GITLEAKS_CONFIG": "/x/custom.toml"}
    )
    assert res.returncode == 0, res.stderr
    (call,) = rig.scanner_calls()
    assert call[call.index("--config") + 1] == "/x/custom.toml"
