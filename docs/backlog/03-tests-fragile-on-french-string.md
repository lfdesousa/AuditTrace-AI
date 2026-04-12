---
title: "test(chat): replace 'Profil' string assertion with structural check"
labels: ["test", "tech-debt"]
priority: P4
---

## Context

`tests/test_chat_proxy.py` and `tests/test_routes.py` assert that the
augmented system message contains the literal string `"Profil"`:

```python
assert "Profil" in system_msg["content"]
```

This is the French heading from one of the procedural memory skill files
(`memory/procedural/SKILL-PITCHING.md` or similar). The assertion is brittle:

- A reword of the skill file (translation, formatting change) silently breaks
  the test for reasons unrelated to the proxy logic under test.
- A new contributor reading the test has zero context for *why* "Profil" —
  it looks like a magic string.
- Localising the skills to English in the future would break this without
  warning.

## Fix sketch

Assert on the *structural* property the test actually cares about:

```python
# The augmented system message should contain BOTH the original instructions
# AND injected memory context. We don't care what the memory content says,
# only that something was injected.
assert "## Agent Instructions" in system_msg["content"]
assert len(system_msg["content"]) > len("You are a helpful assistant.")
assert system_msg["content"].endswith("You are a helpful assistant.")
```

Or, even better — mock the context builder to return a sentinel string and
assert the sentinel landed in the system message. The `client` fixture
already uses a test container; injecting a fake `ContextBuilderService`
should be straightforward.

## Acceptance criteria

- All tests that currently grep for `"Profil"` are rewritten to use a
  structural assertion or a mocked sentinel.
- No test depends on the literal text content of any file under
  `memory/procedural/`.
