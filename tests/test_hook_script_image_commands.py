"""LC10A-HOOK-AWK: the post-upgrade hook script may only call commands that the
pinned hook image actually provides.

Defect (v1.33.0-rc1 deploy, 2026-11-13): ``chat_aud_resolve_scope`` piped kcadm
CSV into ``awk``. The hook Job runs inside ``quay.io/keycloak/keycloak:24.0``,
which has no awk (the script's own comment says "no awk, no jq, no curl"), so the
hook died with ``awk: command not found`` and helm marked the release FAILED.
Every existing test runs the script on the HOST, where awk exists, so none could
see it. This guard reads the RENDERED script statically and refuses any command
word that is not on the allow-list MEASURED from the image.

Neuters (RED, then GREEN on restore, recorded in the build record): put ``awk``
on the allow-list; make ``unlisted_commands`` return an empty list; revert the
scanner's wrapper handling (every ``TestWrappedFormsAreCaught`` row goes RED);
change the hook image repository, tag or template (the rendered-image test goes
RED). The runtime twin is ``tests/test_hook_script_image_runtime.py``.

Limit, stated plainly: the image is referenced by the floating tag ``24.0``, so
a re-push of that tag with different contents is NOT detected here. Pinning by
digest is an operator decision (backlog KEYCLOAK-HOOK-IMAGE-DIGEST-PIN).
"""

from __future__ import annotations

import functools

import pytest

from tests._shell_commands import NON_LITERAL, scan_commands
from tests.test_chart_rendering import _find_workload, _render

# MEASURED, not assumed: `docker run --entrypoint /bin/bash
# quay.io/keycloak/keycloak:24.0` on 2026-11-13, image
# sha256:f8ade94c1d0ad2f2fa7734a455fee5392764f402c43ca35e9af6bf63a2541dc9
# (RepoDigest quay.io/keycloak/keycloak@sha256:f8ade94c...41dc9), bash 5.1.8.
# Output: ~/work/audittrace-private/evidence/2026-11-13-lc10a-hook-awk/measured.txt
# Measured MISSING in that image: awk gawk mawk jq curl wget python python3 perl
# diff rev xxd hostname ps column.
MEASURED_IMAGE_REF = "quay.io/keycloak/keycloak:24.0"
IMAGE_EXTERNALS = frozenset(
    {
        "bash",
        "cat",
        "date",
        "grep",
        "mktemp",
        "paste",
        "rm",
        "sed",
        "sleep",
        "sort",
        "timeout",
        "/opt/keycloak/bin/kcadm.sh",
    }
)
IMAGE_BUILTINS = frozenset(
    {
        ".",
        "[",
        "break",
        "command",
        "continue",
        "declare",
        "echo",
        "exec",
        "exit",
        "kill",
        "local",
        "printf",
        "read",
        "return",
        "set",
        "shift",
        "trap",
        "true",
        "unset",
        "wait",
    }
)
ALLOWED = IMAGE_EXTERNALS | IMAGE_BUILTINS


def unlisted_commands(script: str, allowed: frozenset[str] = ALLOWED) -> list[str]:
    """Every command word in ``script`` that is neither allowed nor defined by it."""
    scan = scan_commands(script)
    return sorted(
        {
            f"{w} (line {n})"
            for n, w in scan.commands
            if w not in allowed | scan.functions
        }
    )


@functools.cache
def _rendered_hook_script() -> str:
    out = _render(["--set", "vault.enabled=true"])
    cm = _find_workload(out, "ConfigMap", "audittrace-memory-scopes-script")
    script = cm["data"]["ensure-memory-scopes.sh"]
    assert isinstance(script, str)
    return script


class TestHookScriptUsesOnlyImageCommands:
    def test_the_rendered_hook_job_image_is_the_measured_one(self) -> None:
        """The RENDERED Job's container image (repository, tag and template all
        covered) must be the image the allow-list was measured on."""
        out = _render(["--set", "vault.enabled=true"])
        job = _find_workload(out, "Job", "audittrace-ensure-memory-scopes")
        images = [c["image"] for c in job["spec"]["template"]["spec"]["containers"]]
        assert images == [MEASURED_IMAGE_REF], (
            "the hook image changed: re-measure the allow-list in this file"
        )

    def test_no_command_outside_the_measured_allow_list(self) -> None:
        script = _rendered_hook_script()
        assert len(scan_commands(script).commands) > 300, "scanner lost sync: vacuous"
        assert unlisted_commands(script) == []

    def test_an_awk_call_injected_into_the_rendered_script_is_caught(self) -> None:
        script = _rendered_hook_script() + "\nx=$(printf a,b | awk -F, '{print $1}')\n"
        assert unlisted_commands(script) == [f"awk (line {len(script.splitlines())})"]

    def test_awk_is_not_on_the_allow_list(self) -> None:
        assert "awk" not in ALLOWED


# Every form the review table named, appended to the REAL rendered script: each
# must be reported (the in-suite neuter: revert the scanner's wrapper handling
# and every row below goes RED).
WRAPPED_FORMS = {
    "command": "x=$(printf a | command awk 1)",
    "command -p": "x=$(printf a | command -p awk 1)",
    "exec": "x=$(printf a | (exec awk 1))",
    "bare exec": "exec awk 1",
    "exec -a": "exec -a name awk 1",
    "bash -c": "x=$(printf a | bash -c 'awk 1')",
    "bash -c double quoted": 'bash -c "printf a | awk 1"',
    "timeout then bash -c": "timeout 5 bash -c 'awk 1'",
    "trap": "trap 'awk 1' EXIT",
    "unquoted heredoc $()": "cat <<EOF\n$(awk 1)\nEOF",
    "unquoted heredoc backtick": "cat <<EOF\n`awk 1`\nEOF",
    "unquoted heredoc nested": "cat <<-EOF\n\t${y:-$(awk 1)}\n\tEOF",
    "bash heredoc": "bash <<'EOF'\nawk 1\nEOF",
    "bash here-string": "bash <<< 'awk 1'",
    "arith with $()": "x=$(( $(awk 1) + 1 ))",
    "arith command": "(( $(awk 1) ))",
}


class TestWrappedFormsAreCaught:
    @pytest.mark.parametrize("form", sorted(WRAPPED_FORMS))
    def test_the_form_injected_into_the_rendered_script_is_reported(
        self, form: str
    ) -> None:
        base = _rendered_hook_script()
        script = base + "\n" + WRAPPED_FORMS[form] + "\n"
        found = unlisted_commands(script)
        assert len(found) == 1 and found[0].startswith("awk (line "), (form, found)

    @pytest.mark.parametrize(
        "snippet",
        [
            "command -v awk",
            "command -V awk",
            "exec 3>&1 4>&2",
            "(exec 4<>/dev/tcp/127.0.0.1/15020)",
            "trap - EXIT",
            "trap 'helper' TERM",
            "cat <<'EOF'\n$(awk 1)\nEOF",
            "cat <<EOF\nplain text with awk and $HOME\nEOF",
            'timeout 5 bash -c "exec 3<>/dev/tcp/${H}/${P}"',
            "echo 'awk' \"$(echo awk)\"",
        ],
    )
    def test_lookups_data_and_declared_helpers_are_not_flagged(
        self, snippet: str
    ) -> None:
        script = "helper() { echo hi; }\n" + snippet + "\n"
        assert unlisted_commands(script) == []

    @pytest.mark.parametrize(
        "snippet", ['bash -c "$payload"', "bash -c $payload", 'trap "$action" EXIT']
    )
    def test_a_non_literal_c_or_trap_string_is_refused(self, snippet: str) -> None:
        assert unlisted_commands(snippet + "\n") == [f"{NON_LITERAL} (line 1)"]


class TestScannerFindsCommandWords:
    @pytest.mark.parametrize(
        ("snippet", "expected"),
        [
            ("echo a | awk '{print}'", ["echo", "awk"]),
            ("x=$(cmd1 | awk 1)", ["cmd1", "awk"]),
            ('x="$(cmd1 | awk 1)"', ["cmd1", "awk"]),
            ("x=`cmd1 | awk 1`", ["cmd1", "awk"]),
            ("a && awk 1 || jq .", ["a", "awk", "jq"]),
            ("if awk 1; then jq .; fi", ["awk", "jq"]),
            ("LC_ALL=C sort <(awk 1)", ["sort", "awk"]),
            ("for f in awk jq; do cat; done", ["cat"]),
            ("while read -r l; do curl; done < f", ["read", "curl"]),
            ('timeout -k 5 "${T:-20}" /bin/tool "$@" &', ["timeout", "/bin/tool"]),
            ("timeout 5 awk 1", ["timeout", "awk"]),
            ("f() { awk 1; }", ["awk"]),
            ("{ awk 1; }", ["awk"]),
            ("! awk 1", ["awk"]),
            ("echo > out 2>&1", ["echo"]),
            ("echo >&2", ["echo"]),
            ('exec 3>&1; cat <<< "awk"', ["exec", "cat"]),
            ("[[ a == b && c ]] && awk 1", ["awk"]),
            ("(( i++ )); awk 1", ["awk"]),
            ("for ((i = 0; i < 3; i++)); do awk 1; done", ["awk"]),
            ("x=$((1 + 2)); awk 1", ["awk"]),
            ("x=${y:-$(awk 1)}", ["awk"]),
            ('x="${y:-$(awk 1)}"', ["awk"]),
            ("a=( awk jq # c\n curl ); cat", ["cat"]),
            ("echo a \\\n | awk 1", ["echo", "awk"]),
            ("command awk 1", ["command", "awk"]),
            ("command -v awk", ["command"]),
            ("bash -c 'awk 1'", ["bash", "awk"]),
            ("trap 'awk 1' EXIT", ["trap", "awk"]),
        ],
    )
    def test_command_words(self, snippet: str, expected: list[str]) -> None:
        assert [w for _n, w in scan_commands(snippet).commands] == expected

    def test_comments_quotes_and_heredoc_bodies_are_not_commands(self) -> None:
        script = (
            "# awk in a comment\n"
            "echo 'awk in single quotes' \"awk in double quotes\"\n"
            "cat <<'EOF'\nawk 1\n  jq .\nEOF\n"
            "cat <<-EOT\n\tawk 1\n\tEOT\n"
            "echo done\n"
        )
        assert [w for _n, w in scan_commands(script).commands] == [
            "echo",
            "cat",
            "cat",
            "echo",
        ]

    def test_case_arms_patterns_are_not_commands_but_bodies_are(self) -> None:
        script = (
            'case "$x" in\n'
            "  124) awk 1 ;;\n"
            "  'a' | b*) echo ;;\n"
            "  *) jq . ;;\n"
            "esac\n"
            "cat\n"
        )
        assert [w for _n, w in scan_commands(script).commands] == [
            "awk",
            "echo",
            "jq",
            "cat",
        ]

    def test_script_defined_functions_are_recorded_and_allowed(self) -> None:
        script = "helper() {\n  echo hi\n}\nhelper\nawk 1\n"
        scan = scan_commands(script)
        assert scan.functions == {"helper"}
        assert unlisted_commands(script, frozenset({"echo"})) == ["awk (line 5)"]

    def test_line_numbers_are_one_based_and_survive_heredocs(self) -> None:
        script = "cat <<EOF\nx\nEOF\nawk 1\n"
        assert scan_commands(script).commands == [(1, "cat"), (4, "awk")]
