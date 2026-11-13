"""A small static scanner: the command words a bash script would exec.

Purpose: the hook-image guard (``tests/test_hook_script_image_commands.py``)
needs to know which EXTERNAL commands a rendered hook script invokes, without
running it (running it on the host is exactly how ``awk`` went unnoticed: the
host has awk, the pinned Keycloak image does not).

It is deliberately conservative, not a full bash parser. It understands
comments, single and double quotes, ``$(...)``, backticks, ``<(...)``, heredocs,
redirections, ``[[ ... ]]``, ``(( ... ))``, ``case`` arms, ``for ... in`` lists,
leading ``VAR=value`` assignments, ``name() {`` definitions and the
control-flow keywords. A command word that is not a plain literal (``"${X}"``)
is reported verbatim so the allow-list must name it explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_KEEP_CMD = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"}
_NO_CMD = {"for", "select", "in", "function"}
_CLOSERS = {"fi", "done", "esac", "}"}
_REDIR = re.compile(r"^[0-9]*(>>|>\||>&|&>>|&>|<<<|<<-|<<|<&|<>|>|<)")


@dataclass
class Scan:
    """Result: command words by line, and the functions the script defines."""

    commands: list[tuple[int, str]] = field(default_factory=list)
    functions: set[str] = field(default_factory=set)


class _Scanner:
    def __init__(self, text: str) -> None:
        self.t = text
        self.i = 0
        self.out = Scan()
        self.heredocs: list[tuple[str, bool]] = []
        self._seen_duration = False

    def line(self, pos: int) -> int:
        return self.t.count("\n", 0, pos) + 1

    # -- low-level readers -------------------------------------------------
    def _skip_heredocs(self) -> None:
        # self.i is just past a newline; consume each pending heredoc body.
        for delim, strip in self.heredocs:
            while self.i < len(self.t):
                end = self.t.find("\n", self.i)
                end = len(self.t) if end < 0 else end
                ln = self.t[self.i : end]
                self.i = min(end + 1, len(self.t))
                if (ln.lstrip("\t") if strip else ln) == delim:
                    break
        self.heredocs = []

    def _paren_body(self, opener: int) -> None:
        """self.i is just past ``(`` of ``$(``/``<(``: scan to the matching ``)``."""
        self._list(closer=")")
        assert self.t[self.i - 1 : self.i] == ")" or self.i >= len(self.t), opener

    def _word(self) -> str:
        """Read one shell word; nested ``$(...)`` commands are collected."""
        t, buf = self.t, []
        while self.i < len(t):
            c = t[self.i]
            if c == "(" and re.search(r"[A-Za-z0-9_\]]\+?=$", "".join(buf)):
                self._array_literal()
                buf.append("(..)")
                continue
            if c in " \t\n;&|()<>" and not (
                c in "<>" and t[self.i + 1 : self.i + 2] == "("
            ):
                break
            if c == "\\":
                buf.append(t[self.i : self.i + 2])
                self.i += 2
            elif c == "'":
                end = t.index("'", self.i + 1)
                buf.append(t[self.i : end + 1])
                self.i = end + 1
            elif c == '"':
                buf.append(self._dquote())
            elif c == "`":
                end = t.index("`", self.i + 1)
                sub = _Scanner(t[self.i + 1 : end])
                sub._list(closer=None)
                self._merge(sub, self.line(self.i) - 1)
                buf.append("`..`")
                self.i = end + 1
            elif c == "$" and t[self.i + 1 : self.i + 3] == "((":
                buf.append(self._arith())
            elif c == "$" and t[self.i + 1 : self.i + 2] == "(":
                self.i += 2
                self._paren_body(self.i)
                buf.append("$(..)")
            elif c == "$" and t[self.i + 1 : self.i + 2] == "{":
                end = self._brace_end(self.i + 2)
                buf.append(t[self.i : end])
                self.i = end
            elif c in "<>":  # process substitution <( / >(
                self.i += 2
                self._paren_body(self.i)
                buf.append("<(..)")
            else:
                buf.append(c)
                self.i += 1
        return "".join(buf)

    def _array_literal(self) -> None:
        """``NAME=( word ... )``: the words are data, never commands."""
        self.i += 1
        while self.i < len(self.t) and self.t[self.i] != ")":
            c = self.t[self.i]
            if c in " \t\n":
                self.i += 1
            elif c == "#":
                end = self.t.find("\n", self.i)
                self.i = len(self.t) if end < 0 else end
            elif not self._word():
                self.i += 1
        self.i += 1

    def _merge(self, sub: _Scanner, base: int) -> None:
        self.out.commands += [(base + n, w) for n, w in sub.out.commands]
        self.out.functions |= sub.out.functions

    def _brace_end(self, i: int) -> int:
        depth = 1
        while i < len(self.t) and depth:
            if self.t[i] == "{":
                depth += 1
            elif self.t[i] == "}":
                depth -= 1
            elif self.t[i] == "$" and self.t[i + 1 : i + 2] == "(":
                save = self.i
                self.i = i + 2
                self._paren_body(self.i)
                i = self.i - 1
                self.i = save
            i += 1
        return i

    def _arith(self) -> str:
        start, depth = self.i, 0
        while self.i < len(self.t):
            if self.t.startswith("((", self.i):
                depth += 1
                self.i += 2
            elif self.t.startswith("))", self.i):
                depth -= 1
                self.i += 2
                if depth == 0:
                    break
            else:
                self.i += 1
        return self.t[start : self.i]

    def _dquote(self) -> str:
        t, start = self.t, self.i
        self.i += 1
        while self.i < len(t) and t[self.i] != '"':
            if t[self.i] == "\\":
                self.i += 2
            elif t.startswith("$((", self.i):
                self._arith()
            elif t.startswith("$(", self.i):
                self.i += 2
                self._paren_body(self.i)
            elif t.startswith("${", self.i):
                self.i = self._brace_end(self.i + 2)
            elif t[self.i] == "`":
                end = t.index("`", self.i + 1)
                sub = _Scanner(t[self.i + 1 : end])
                sub._list(closer=None)
                self._merge(sub, self.line(self.i) - 1)
                self.i = end + 1
            else:
                self.i += 1
        self.i += 1
        return t[start : self.i]

    # -- the command-list state machine ------------------------------------
    def _list(self, closer: str | None) -> None:
        t = self.t
        expect = True
        in_dbl_bracket = False
        in_list_words = False  # after for/select: words up to ; or newline
        cases: list[str] = []  # per open case: subject | pattern | body
        wrapper = ""  # "timeout" while its options/duration are being skipped
        while self.i < len(t):
            c = t[self.i]
            if c == "\n":
                self.i += 1
                self._skip_heredocs()
                expect, in_list_words = True, False
                wrapper = ""
                continue
            if c in " \t":
                self.i += 1
                continue
            if c == "#":
                end = t.find("\n", self.i)
                self.i = len(t) if end < 0 else end
                continue
            if c == "\\" and t[self.i + 1 : self.i + 2] == "\n":
                self.i += 2
                continue
            if in_dbl_bracket:
                w = self._word()
                if w == "]]":
                    in_dbl_bracket, expect = False, False
                elif not w:
                    self.i += 1  # lone operator char inside [[ ]]
                continue
            if c == ")":
                self.i += 1
                if cases and cases[-1] == "pattern":
                    cases[-1], expect = "body", True
                    continue
                if closer == ")":
                    return
                expect = True  # a case-arm pattern terminator
                continue
            if c == "(":
                self.i += 1
                if t[self.i : self.i + 1] == "(":
                    self.i -= 1  # _arith counts from the opening ``((``
                    self._arith()
                    expect = False
                else:
                    expect = True
                continue
            if c in ";&|":
                if t.startswith(";;", self.i) and cases and cases[-1] == "body":
                    cases[-1] = "pattern"
                self.i += 2 if t.startswith(";;", self.i) else 1
                expect, in_list_words, wrapper = True, False, ""
                continue
            m = _REDIR.match(t[self.i : self.i + 12])
            if m and not _ASSIGN.match(t[self.i :]):
                op = m.group(0)
                self.i += len(op)
                if op.endswith("&"):  # fd dup: >&2 / <&-
                    self._word()
                    continue
                while t[self.i : self.i + 1] in " \t":
                    self.i += 1
                if "<<" in op and "<<<" not in op:
                    delim = self._word().strip("'\"\\")
                    self.heredocs.append((delim, op.endswith("-")))
                else:
                    self._word()  # a filename / here-string: never a command
                continue
            pos = self.i
            w = self._word()
            if not w:
                self.i += 1
                continue
            if cases and cases[-1] in ("subject", "pattern"):
                if cases[-1] == "subject" and w == "in":
                    cases[-1] = "pattern"
                elif w == "esac":
                    cases.pop()
                    expect = False
                continue
            if wrapper:
                if w.startswith("-") or w == "-k" or wrapper == "arg":
                    wrapper = "arg" if w in ("-k", "-s") else "timeout"
                elif wrapper == "timeout" and not self._seen_duration:
                    self._seen_duration = True  # the DURATION positional
                else:
                    wrapper, self._seen_duration = "", False
                    self.out.commands.append((self.line(pos), w))
                    expect = False
                continue
            if not expect or in_list_words:
                continue
            if t[self.i : self.i + 2] == "()":  # name() definition
                self.out.functions.add(w)
                self.i += 2
                continue
            if _ASSIGN.match(w):
                continue  # still in command position
            if w == "[[":
                in_dbl_bracket, expect = True, False
                continue
            if w in _KEEP_CMD:
                continue
            if w in _NO_CMD:
                if w in ("for", "select", "in"):
                    in_list_words = True
                continue
            if w == "case":
                cases.append("subject")
                continue
            if w in _CLOSERS:
                expect = False
                continue
            self.out.commands.append((self.line(pos), w))
            expect = False
            if w == "timeout":
                wrapper, self._seen_duration = "timeout", False


def scan_commands(script: str) -> Scan:
    """Return every command word of ``script`` with its 1-based line."""
    s = _Scanner(script)
    s._list(closer=None)
    return s.out
