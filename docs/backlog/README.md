# Backlog — known issues awaiting GitHub import

This directory holds one markdown file per known issue/smell that has been
*identified but deferred*. Each file is structured for direct import to
GitHub Issues via the `gh` CLI.

The contents are intentionally tracked in the repo (not just in someone's
memory) so a fresh contributor — or future-you after a long context break —
can see the full deferred-work surface in one place.

## Why files in the repo?

When these issues were first catalogued (2026-04-11) the working machine had
no `gh` CLI installed and no GitHub token in the environment, so creating
issues directly was not possible. Documenting them here keeps the work
trackable today and lets us bulk-create them on GitHub later.

## How to import to GitHub once `gh` is available

```bash
# One-time install + auth
sudo apt install gh
gh auth login

# Bulk-create from this directory
cd docs/backlog
./create-issues.sh
```

Or paste the body of an individual file into GitHub's web UI under
**Issues → New issue** if you prefer to triage one at a time.

## File naming convention

`NN-short-kebab-title.md` where `NN` is the order in which the issue was
catalogued. The number has no priority meaning — sort by the `priority:`
field in the file's front matter for that.

## Front matter schema

Every file starts with YAML front matter parsed by `create-issues.sh`:

```yaml
---
title: "feat: short imperative title"
labels: ["bug", "tech-debt"]
priority: P3
---
```

`title`, `labels`, and `priority` are required. Body follows the front matter.
