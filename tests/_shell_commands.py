"""A small static scanner: the command words a bash script would exec.

Purpose: the hook-image guard (``tests/test_hook_script_image_commands.py``)
needs to know which EXTERNAL commands a rendered hook script invokes, without
running it (running it on the host is exactly how ``awk`` went unnoticed: the
host has awk, the pinned Keycloak image does not).

It is deliberately conservative, not a full bash parser. It understands
comments, single and double quotes, ``$(...)``, ``$((...))``, backticks,
``<(...)``, heredocs, redirections, ``[[ ... ]]``, ``(( ... ))``, ``case``
arms, ``for ... in`` lists, leading ``VAR=value`` assignments, ``name() {``
definitions and the control-flow keywords. A command word that is not a plain
literal (``"${X}"``) is reported verbatim so the allow-list must name it.

Wrapped commands (the words that take a command as an argument) are followed:

* ``timeout [opts] DURATION cmd``, ``command [-p] cmd`` (``-v``/``-V`` only
  look a name up and run nothing), ``exec [-cl] [-a name] cmd``;
* ``bash -c 'script'`` / ``sh -c "script"``: the literal string is scanned as a
  script; a non-literal one (a bare variable) is reported as
  ``<non-literal -c/trap string>``;
* ``trap 'script' SIG``: the same rule for the action string;
* an UNQUOTED heredoc body runs ``$(...)`` / backticks / ``$((...))`` at
  expansion time, so those are scanned; a heredoc fed straight to a shell
  (``bash <<'EOF'``, ``bash <<< "..."``) is scanned as a script.

Not scannable statically, covered only where the runtime guard
(``tests/test_hook_script_image_runtime.py``) exercises the path: a script piped
into a shell (``cat <<'EOF' | bash``), ``eval``, ``source`` of a generated file,
``env``/``xargs``/``nohup`` wrappers (they are reported as themselves and are not
on the allow-list), and any command whose NAME is built at run time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_KEEP_CMD = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"}
_NO_CMD = {"for", "select", "in", "function"}
_CLOSERS = {"fi", "done", "esac", "}"}
_REDIR = re.compile(r"^[0-9]*(>>|>\||>&|&>>|&>|<<<|<<-|<<|<&|<>|>|<)")
_SHELLS = {"bash", "sh", "/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/sh"}
_WRAPPERS = {"timeout", "command", "exec"}
_DASH_C = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
_PURE_VAR = re.compile(r"^\$(\w+|\{[^}]*\}|[@*])$")
NON_LITERAL = "<non-literal -c/trap string>"


@dataclass
class Scan:
    """Result: command words by line, and the functions the script defines."""

    commands: list[tuple[int, str]] = field(default_factory=list)
    functions: set[str] = field(default_factory=set)


@dataclass
class _Frame:
    """Per-command-list parse state (a ``$(...)`` opens a fresh frame)."""

    expect: bool = True  # next word is in command position
    dbl: bool = False  # inside [[ ... ]]
    list_words: bool = False  # after for/select: words up to ; or newline
    cases: list[str] = field(default_factory=list)  # subject | pattern | body
    wrap: str = ""  # a pending wrapper: timeout | command | exec
    seen_duration: bool = False
    skip_arg: bool = False  # the next word is an option's argument
    shell: str = ""  # "shell" (bash/sh) | "trap": the command takes a string
    stdin_shell: bool = False  # a shell with neither -c nor a script file
    shell_c: bool = False  # -c seen: the next word is the script


def _literal_inner(raw: str) -> str | None:
    """The text of a fully quoted/plain word, or None when it is not literal."""
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'" and "'" not in raw[1:-1]:
        inner = raw[1:-1]
    elif len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        inner = re.sub(r'\\(["\\$`])', r"\1", raw[1:-1])
    elif not any(ch in raw for ch in "$`'\""):
        inner = raw
    else:
        return None
    return None if _PURE_VAR.match(inner.strip()) else inner


class _Scanner:
    def __init__(self, text: str) -> None:
        self.t = text
        self.i = 0
        self.out = Scan()
        self.heredocs: list[tuple[str, bool, str]] = []
        self.f = _Frame()

    def line(self, pos: int) -> int:
        return self.t.count("\n", 0, pos) + 1

    # -- nested scripts ----------------------------------------------------
    def _merge(self, sub: _Scanner, base: int) -> None:
        self.out.commands += [(base + n, w) for n, w in sub.out.commands]
        self.out.functions |= sub.out.functions

    def _scan_script(self, text: str, first_line: int) -> None:
        sub = _Scanner(text)
        sub._list(closer=None)
        self._merge(sub, first_line - 1)

    def _scan_string(self, raw: str, pos: int) -> None:
        """A ``-c`` / ``trap`` string: scan it, or report it when not literal."""
        if raw in ("-", "''", '""'):  # trap - SIG / an empty action
            return
        inner = _literal_inner(raw)
        if inner is None:
            self.out.commands.append((self.line(pos), NON_LITERAL))
            return
        self._scan_script(inner, self.line(pos))

    def _scan_expansions(self, text: str, first_line: int) -> None:
        """Command substitutions inside ``text`` that is NOT itself a script
        (a heredoc body, an arithmetic expression): ``$(..)``, backticks."""
        j = 0
        while j < len(text):
            here = first_line + text.count("\n", 0, j)
            if text[j] == "\\":
                j += 2
            elif text[j] == "`":
                end = text.find("`", j + 1)
                end = len(text) if end < 0 else end
                self._scan_script(text[j + 1 : end], here)
                j = end + 1
            elif text.startswith("$((", j):
                j += 3  # the arithmetic body is scanned by the same loop
            elif text.startswith("$(", j):
                sub = _Scanner(text[j + 2 :])
                sub._list(closer=")")
                self._merge(sub, here - 1)
                j += 2 + sub.i
            else:
                j += 1

    # -- low-level readers -------------------------------------------------
    def _skip_heredocs(self) -> None:
        # self.i is just past a newline; consume each pending heredoc body.
        for delim, strip, mode in self.heredocs:
            start = body_end = self.i
            while self.i < len(self.t):
                end = self.t.find("\n", self.i)
                end = len(self.t) if end < 0 else end
                ln = self.t[self.i : end]
                body_end, self.i = self.i, min(end + 1, len(self.t))
                if (ln.lstrip("\t") if strip else ln) == delim:
                    break
            else:
                body_end = self.i  # unterminated: the whole rest is the body
            body = self.t[start:body_end]
            if mode == "script":
                self._scan_script(body, self.line(start))
            elif mode == "expand":
                self._scan_expansions(body, self.line(start))
        self.heredocs = []

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
            buf.append(self._word_part(c))
        return "".join(buf)

    def _word_part(self, c: str) -> str:
        """Consume one quoted/expanded unit (or one plain char) at self.i."""
        t, start = self.t, self.i
        if c == "\\":
            self.i += 2
        elif c == "'":
            self.i = t.index("'", self.i + 1) + 1
        elif c == '"':
            self._dquote()
        elif c == "`":
            end = t.index("`", self.i + 1)
            self._scan_script(t[self.i + 1 : end], self.line(self.i))
            self.i = end + 1
        elif c == "$" and t[self.i + 1 : self.i + 3] == "((":
            self._arith()
        elif c == "$" and t[self.i + 1 : self.i + 2] == "(":
            self.i += 2
            self._list(closer=")")
        elif c == "$" and t[self.i + 1 : self.i + 2] == "{":
            self.i = self._brace_end(self.i + 2)
        elif c in "<>":  # process substitution <( / >(
            self.i += 2
            self._list(closer=")")
        else:
            self.i += 1
        return t[start : self.i]

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
                self._list(closer=")")
                i = self.i - 1
                self.i = save
            i += 1
        return i

    def _arith(self) -> None:
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
        self._scan_expansions(self.t[start : self.i], self.line(start))

    def _dquote(self) -> None:
        t = self.t
        self.i += 1
        while self.i < len(t) and t[self.i] != '"':
            if t[self.i] == "\\":
                self.i += 2
            elif t.startswith("$((", self.i):
                self._arith()
            elif t.startswith("$(", self.i):
                self.i += 2
                self._list(closer=")")
            elif t.startswith("${", self.i):
                self.i = self._brace_end(self.i + 2)
            elif t[self.i] == "`":
                end = t.index("`", self.i + 1)
                self._scan_script(t[self.i + 1 : end], self.line(self.i))
                self.i = end + 1
            else:
                self.i += 1
        self.i += 1

    # -- the command-list state machine ------------------------------------
    def _list(self, closer: str | None) -> None:
        outer, self.f = self.f, _Frame()
        try:
            while self.i < len(self.t):
                if self._trivia():
                    continue
                if self.f.dbl:
                    self._dbl_bracket_word()
                    continue
                c = self.t[self.i]
                if c == ")":
                    self.i += 1
                    if self._close_paren(closer):
                        return
                elif c == "(":
                    self._open_paren()
                elif c in ";&|":
                    self._separator()
                elif not self._redirect():
                    self._word_in_list()
        finally:
            self.f = outer

    def _trivia(self) -> bool:
        """Newlines, blanks, comments, line continuations. True if consumed."""
        t, c = self.t, self.t[self.i]
        if c == "\n":
            self.i += 1
            self._skip_heredocs()
            self._new_command()
        elif c in " \t":
            self.i += 1
        elif c == "#":
            end = t.find("\n", self.i)
            self.i = len(t) if end < 0 else end
        elif c == "\\" and t[self.i + 1 : self.i + 2] == "\n":
            self.i += 2
        else:
            return False
        return True

    def _new_command(self) -> None:
        f = self.f
        f.expect, f.list_words, f.wrap = True, False, ""
        f.shell, f.stdin_shell, f.shell_c, f.skip_arg = "", False, False, False

    def _dbl_bracket_word(self) -> None:
        w = self._word()
        if w == "]]":
            self.f.dbl, self.f.expect = False, False
        elif not w:
            self.i += 1  # a lone operator char inside [[ ]]

    def _close_paren(self, closer: str | None) -> bool:
        f = self.f
        if f.cases and f.cases[-1] == "pattern":
            f.cases[-1], f.expect = "body", True
            return False
        if closer == ")":
            return True
        f.expect = True
        return False

    def _open_paren(self) -> None:
        self.i += 1
        if self.t[self.i : self.i + 1] == "(":
            self.i -= 1  # _arith counts from the opening ``((``
            self._arith()
            self.f.expect = False
        else:
            self.f.expect = True

    def _separator(self) -> None:
        f = self.f
        if self.t.startswith(";;", self.i):
            if f.cases and f.cases[-1] == "body":
                f.cases[-1] = "pattern"
            self.i += 2
        else:
            self.i += 1
        self._new_command()

    def _redirect(self) -> bool:
        t = self.t
        m = _REDIR.match(t[self.i : self.i + 12])
        if not m or _ASSIGN.match(t[self.i :]):
            return False
        op = m.group(0)
        self.i += len(op)
        if op.endswith("&"):  # fd dup: >&2 / <&-
            self._word()
            return True
        while t[self.i : self.i + 1] in " \t":
            self.i += 1
        pos = self.i
        raw = self._word()  # a filename / here-doc delimiter / here-string
        if op.endswith("<<<"):
            if self.f.stdin_shell:
                self._scan_string(raw, pos)
        elif op.endswith("<<") or op.endswith("<<-"):
            quoted = any(ch in raw for ch in "'\"\\")
            mode = "script" if self.f.stdin_shell else ("data" if quoted else "expand")
            self.heredocs.append((raw.strip("'\"\\"), op.endswith("-"), mode))
        return True

    def _record(self, pos: int, w: str) -> None:
        f = self.f
        self.out.commands.append((self.line(pos), w))
        f.expect = False
        if w in _SHELLS:
            f.shell, f.stdin_shell = "shell", True
        elif w == "trap":
            f.shell = "trap"
        elif w in _WRAPPERS:
            f.wrap, f.seen_duration = w, False

    def _word_in_list(self) -> None:
        pos = self.i
        w = self._word()
        if not w:
            self.i += 1
            return
        f = self.f
        if f.cases and f.cases[-1] in ("subject", "pattern"):
            self._case_word(w)
        elif f.shell:
            self._shell_arg(w, pos)
        elif f.wrap:
            self._wrapper_arg(w, pos)
        elif f.expect and not f.list_words:
            self._command_position(w, pos)

    def _case_word(self, w: str) -> None:
        f = self.f
        if f.cases[-1] == "subject" and w == "in":
            f.cases[-1] = "pattern"
        elif w == "esac":
            f.cases.pop()
            f.expect = False

    def _shell_arg(self, w: str, pos: int) -> None:
        f = self.f
        if f.shell == "trap":
            if w == "-" or not w.startswith("-"):  # the first operand: the action
                f.shell = ""
                self._scan_string(w, pos)
        elif f.shell_c:
            f.shell, f.shell_c = "", False
            self._scan_string(w, pos)
        elif _DASH_C.match(w):
            f.shell_c, f.stdin_shell = True, False
        elif not w.startswith("-"):
            f.stdin_shell = False  # a script FILE: its text is not visible here

    def _wrapper_arg(self, w: str, pos: int) -> None:
        f = self.f
        if f.skip_arg:
            f.skip_arg = False
        elif w.startswith("-"):
            if f.wrap == "command" and re.search(r"[vV]", w):
                f.wrap = ""  # a lookup: nothing is executed
            elif (f.wrap == "timeout" and w in ("-k", "-s")) or (
                f.wrap == "exec" and w == "-a"
            ):
                f.skip_arg = True
        elif f.wrap == "timeout" and not f.seen_duration:
            f.seen_duration = True
        else:
            f.wrap, f.seen_duration = "", False
            self._record(pos, w)

    def _command_position(self, w: str, pos: int) -> None:
        f = self.f
        if self.t[self.i : self.i + 2] == "()":  # name() definition
            self.out.functions.add(w)
            self.i += 2
        elif _ASSIGN.match(w) or w in _KEEP_CMD:
            return  # still in command position
        elif w == "[[":
            f.dbl, f.expect = True, False
        elif w in _NO_CMD:
            f.list_words = w in ("for", "select", "in")
        elif w == "case":
            f.cases.append("subject")
        elif w in _CLOSERS:
            f.expect = False
        else:
            self._record(pos, w)


def scan_commands(script: str) -> Scan:
    """Return every command word of ``script`` with its 1-based line."""
    s = _Scanner(script)
    s._list(closer=None)
    return s.out
