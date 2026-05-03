# Bruno collection — AuditTrace-AI

A complete Bruno collection covering every public-facing endpoint:
OAuth2 Device Flow, OpenAI-compatible chat, the four memory layers
(episodic / procedural / semantic / conversational), the auditor
surface, and the legacy upload + index admin paths.

Bruno (https://www.usebruno.com/) is a Git-friendly, file-based
alternative to Postman — every request is a plain-text `.bru` file
that diffs cleanly. The collection lives under `bruno/audittrace/`
and is opened directly in the Bruno desktop app.

## Setup

1. **Install Bruno** if you haven't already.
   - Linux AppImage:
     ```sh
     mv ~/Téléchargements/bruno_*_x86_64_linux.AppImage ~/.local/bin/bruno
     chmod +x ~/.local/bin/bruno
     bruno
     ```
   - Or download from https://www.usebruno.com/downloads.

2. **Open the collection** in Bruno:
   `File → Open Collection` → navigate to
   `<repo>/bruno/audittrace/`.

3. **Pick an environment** in the top-right dropdown:
   - `cluster` — points at the public cluster
     (`audittrace.allaboutdata.eu:30952`).
   - `local` — for `make k8s-port-forward` setups, points at
     `localhost:8765` + `localhost:8081`.

   Adjust the env file (`environments/<name>.bru`) if your
   hostnames differ.

## Authenticating (one-time per session)

The collection uses the OAuth2 Device Authorization Grant
(RFC 8628) — same flow as `audittrace-login` and the webui's
"Sign in" button.

1. **Run `auth/01 Device Flow — init`.** The post-response script
   stashes `device_code`, `user_code`, and `verification_uri`
   into the env vars and logs the URL to the script panel.

2. **Open `verificationUri` in your browser** (the script logs the
   complete URL with `user_code` pre-filled). Sign in if needed,
   approve the request.

3. **Run `auth/02 Device Flow — poll`.** On success, this stashes
   `accessToken` + `refreshToken` into the env vars. Pre-approval,
   it returns 400 `authorization_pending` — that's normal; just
   re-run after approving in the browser.

4. **Every other request** uses `{{accessToken}}` automatically.

When the token expires, run `auth/03 Refresh access_token` —
no browser round-trip needed.

> **Scope caveat** (PR A live test, 2026-05-03): refresh-token
> grants do NOT widen scopes. If you originally logged in without
> `memory:*:write`, refreshing won't add it. The default scope set
> in the env vars covers everything in the collection — only
> change it if you're testing the negative-auth path.

## Collection layout

```
bruno/audittrace/
├── bruno.json                 — collection manifest
├── environments/              — cluster + local env presets
├── auth/                      — Device Flow (3 steps)
├── health/                    — /health + /metrics (no auth)
├── chat/                      — /v1/models + /v1/chat/completions
├── memory/
│   ├── episodic/              — POST/GET-list/GET-item/PUT/DELETE
│   ├── procedural/            — same 5 verbs
│   ├── semantic/              — same 5 verbs (collection/doc-id keyed)
│   ├── conversational/        — list-sessions + read-session (read-only)
│   ├── upload-legacy.bru      — pre-PR-A upload path (admin)
│   └── index-legacy.bru       — rebuild ChromaDB from MinIO (admin)
└── audit/                     — auditor surface (interactions + sessions)
```

## Running a CRUD smoke

After authenticating, you can run the entire memory CRUD round-trip
end-to-end:

1. `memory/episodic/02 Create` — creates `ADR-bruno-test.md`.
2. `memory/episodic/01 List` — confirm it appears.
3. `memory/episodic/03 Read` — fetch content + manifest.
4. `memory/episodic/04 Update` — bumps `modified_at_ms`.
5. `memory/episodic/05 Delete` — soft-delete.

Same shape for procedural and semantic. The conversational layer is
read-only (sessions are produced by `/v1/chat/completions` itself);
its requests just exercise the GET endpoints.

Each request has assertions in its `tests {}` block. Bruno's runner
shows per-test pass/fail.

## Variables you can edit

In `environments/<env>.bru`:

- `baseUrl`, `realmIssuer`, `clientId` — endpoint targets.
- `scope` — the JWT scope set requested at login. Includes every
  `memory:*` read+write scope by default.
- `exampleAdrFile`, `exampleSkillFile`, `exampleCollection`,
  `exampleDocId` — fixture names used by the create/read/update/
  delete requests. Change them per smoke run if you don't want
  to step on a previous test artefact.
- `accessToken`, `refreshToken`, `deviceCode`, `userCode`,
  `verificationUri`, `exampleSessionId` — populated automatically
  by the `script:post-response` hooks; don't edit by hand.

## CI usage (future)

The collection can be run headless via `bru run`:

```sh
cd bruno/audittrace
bru run --env cluster --output-format junit > junit.xml
```

That hooks into the same per-PR live-evidence gate as
`make verify-deploy`. Not wired yet — TODO once the `bru` CLI
is on the CI image.

## Cross-references

- `webui/index.html` — same endpoints in a browser harness.
- `scripts/audittrace-login` — the CLI Device Flow helper Bruno's
  auth flow mirrors.
- `docs/guides/memory-backoffice.md` — operator narrative for the
  memory layers.
