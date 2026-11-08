# Per-chat session recall (LC-10a)

A note attached in one chat is recalled in that chat only. The `session`
memory layer gains a server-derived `session_id` compartment inside the
caller's own rows; `user_id` stays the principal boundary and Postgres RLS is
unchanged.

## How a request names its chat

* Send `X-Session-Id: <id>` on `POST /memory/upload?layer=session`,
  `POST /v1/chat/completions` and `POST /mcp`.
* Accepted shape: printable ASCII without space, 1 to 255 characters
  (`^[\x21-\x7e]{1,255}$`, matched with `re.fullmatch`). Surrounding spaces are
  trimmed first. A value of any other shape, or one containing `{{` or `}}` (an unresolved template placeholder), is treated as absent; the log
  records its length only.
* The id is correlation and compartment data, never a credential: it is read
  from the header only, never from a tool argument, body field or query
  parameter.

## Behaviour changes

* `recall_attachments` returns only uploads made under the current chat's
  session id.
* An upload with no valid `X-Session-Id` is stored with no compartment and is
  recalled by no chat. Clients that upload to `layer=session` without the header
  (an OpenCode `/memory/upload`, an MCP client) therefore no longer see those
  uploads in a later recall until they send the header on upload and on recall.
  The shipped Bruno requests under `bruno/audittrace/session-recall/`,
  `mcp/03`, `mcp/04` and `memory/upload-legacy.bru` send it.
* Rows written before the migration keep no compartment and age out under the
  24 hour session retention.
* `recall_attachments` is no longer cached: ask, attach, ask sees the new
  attachment in the same turn.
* Rollout order: the fork tag first, then one chart release that pins it and
  carries this change (migration `5b4bd766374f`, BFF forward, the console's
  `X-Session-Id` header line).

## Demo client

`audittrace-session-e2e` (chart flag `keycloak.sessionE2eClient.enabled`,
default on): a public device-flow client, consent required, no refresh token,
900 s access tokens. Default scopes `audittrace:query` and `audittrace:audit`;
`memory:session:write` and `memory:session:read-own` are OPTIONAL and requested
by name through the Bruno environment `cluster-session-e2e`.
