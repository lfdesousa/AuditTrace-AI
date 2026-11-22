# Audit record: image record, content hash, refused requests

This note describes three additions to the audit record (`interactions` rows, returned by
`GET /interactions`). Every field that existed before keeps its name, type and meaning.

## 1. `input_media` (new field, may be null)

The images a chat request carried. An object `{"schema", "count", "items"}`; each item names
one image part of the request: `message_index`, `part_index`, `role`, `source`, `status`, and
for a recorded image its `media_type` (as declared by the client, not checked against the
bytes), `bytes` (decoded size) and `sha256` (of the decoded bytes). A refused image part has
`reason`, `url_chars` and `url_sha256` instead. The image itself is never stored, and neither is
the text of a refused source. `url_sha256` is the SHA-256 of that text: it lets two refusals of
the same source be recognised, and for a short or predictable source it can be matched by guessing,
so read it as an identifier, not as a secret.

`null` means **no image recorded**. It does not always mean that no image was sent:

- rows written before this field existed are `null`, also when the request carried an image;
  they are never back-filled, because audit rows are append-only;
- a chat turn answered by a server of an earlier release (for example during an upgrade) is `null`;
- rows that are not chat turns are always `null`: tool-interface calls, memory-access events,
  scan results, access-control events, assessment rows and refused-turn reports (see `source`).

For a chat turn written by a release that has this field, `null` means the request had no image part.
Only images are recorded; audio and file parts are not.

## 2. `content_hash`: the recipe

`content_hash` is the SHA-256 (hex) of the UTF-8 bytes of
`json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)`, where `payload`
holds these 16 fields (a missing value is JSON `null`):

<!-- hash-fields -->
```text
1. `project`
2. `source`
3. `question`
4. `answer`
5. `prompt_tokens`
6. `completion_tokens`
7. `timestamp`
8. `session_id`
9. `model`
10. `user_id`
11. `status`
12. `failure_class`
13. `error_detail`
14. `duration_ms`
15. `trace_id`
16. `event_class`
```

plus `input_media`, as the stored JSON text, **only when it is not null**. A row whose
`input_media` is null hashes exactly as it did before this field existed: no existing hash changes.

`GET /interactions` returns `input_media` as an object, not as the stored text. The stored text is
exactly `json.dumps(obj, sort_keys=True, separators=(",", ":"))` of that object (Python): keys
sorted, `,` and `:` with no spaces, no line break. The object holds ASCII characters only, so no
escaping choice is involved. Put that string, not the object, into `payload["input_media"]`
before hashing; hashing the object itself gives a different digest.

Worked example. The row:

<!-- example-row -->
```json
{
 "project": "default",
 "source": "librechat",
 "question": "What does this picture show?",
 "answer": "A single dot.",
 "prompt_tokens": 10,
 "completion_tokens": 5,
 "timestamp": "2026-11-20T12:00:00",
 "session_id": "example-session",
 "model": "audittrace-default",
 "user_id": "example-user",
 "status": "success",
 "failure_class": null,
 "error_detail": null,
 "duration_ms": 1200,
 "trace_id": "0123456789abcdef0123456789abcdef",
 "event_class": null
}
```

hashes to:

<!-- example-hash-without-image -->
```text
e3b46a4a8797ccbdc7940ed5b97de3795247d07ee3dd4da16c5256c111ec5b54
```

The same row with this `input_media` text:

<!-- example-input-media -->
```json
{"count":1,"items":[{"bytes":70,"kind":"image","media_type":"image/png","message_index":0,"part_index":1,"role":"user","sha256":"8cf8513dd0fb04730604640ba5a61a5277b0bb0f3f446b5047b7ad17438e0732","source":"data_uri","status":"recorded"}],"schema":1}
```

hashes to:

<!-- example-hash-with-image -->
```text
cf3cf8d2451f8f4d061310d3bd5684192bd2e601988a41a7c00060218e92250d
```

**Verifiers written for the 16-field recipe** keep verifying every row whose `input_media` is
null. For a row that records an image they compute the first hash above, not the stored one, and
report a mismatch although the row is intact. The same false mismatch appears if the server is
rolled back to an earlier release while such rows exist. Update a verifier to the recipe above
before relying on it for rows that record an image. The schema downgrade refuses to remove the
field while any row records an image.

## 3. `failure_class`: one new value for chat turns

Chat turns use a closed set of failure classes:

<!-- chat-failure-classes -->
```text
`proxy_timeout` `upstream_error` `upstream_unreachable` `internal_error` `request_refused`
```

`request_refused` is new: the request was refused before the model was called, with HTTP 400 and
the reason as the error `code`. `error_detail` is `<reason>: message <i> part <j>`, the position
of the refused part. The reasons are:

<!-- request-refused-reasons -->
```text
`image_url_not_supported` `image_data_invalid` `media_link_not_supported`
```

- `image_url_not_supported`: an image part is a link; images are accepted inline only.
- `image_data_invalid`: an image part is not a valid base64 data URL.
- `media_link_not_supported`: another media part (audio, video, file or any part that is not text
  or an image) holds a link (`http://`, `https://` or `file://`). The service does not fetch media.

The text of a refused link is not kept anywhere: not in the audit row, not in traces, not in logs.
Other kinds of rows use their own values in this column; none of them uses `request_refused`.

## 4. Traces: what is removed and what is not

No media payload of any size reaches a span attribute, a log line, the input tag of the entry
span or the trace-ingestion body. An inline image is replaced by its digest. A part of type
`input_audio`, `input_video` or `file` keeps its type and the keys of its schema (`input_audio`
holds `data` and `format`, `file` holds `file_data`, `file_id` and `filename`, an `image_url` holds
`url` and `detail`), and its payload (`data`, `url`, `file_data`) is replaced by a marker that gives
its length. Any other key of that object holds a marker. A part of any type the
service does not know (for example a custom `blob` part) is replaced whole by a marker that
keeps its type name and its number of keys. The same holds for a shape the OpenAI schema
does not allow: a message `content` that is neither a string nor a list (a dict, a number) is
replaced whole by a marker that records its type and size, and a list element that is not a part
object (a bare string, a number, a nested list) is treated as an unknown part. A message that is not an object is replaced whole by a marker that records its container type
and size. A message object keeps only the keys of the OpenAI chat message schema (`role`,
`content`, `name`, `tool_calls`, `tool_call_id`, `refusal`, `function_call`, `audio`); the value of
any other key is replaced by a marker, whether or not the message has a `role`. The `arguments` of
a tool call are text and are traced as written, like any text. The same rule applies to the
request body: a field outside the OpenAI chat-completions request schema (plus `context_query` and
`project`, which the service reads) is replaced by a marker where the request is traced. A marker
keeps the key's name when it is at most 32 plain identifier characters, so a name can carry at most
24 bytes; otherwise the name is `unknown`. These rules apply where the route copies a message (a
`[key, value]` pair that the route turns into an object) as well. How the route answers a
malformed request is not changed by this: some of them already end in an error of their own, and
the telemetry written before that error is clean. The model server
still receives every part unchanged, and the audit record never holds a payload.

The types the service treats as text are `text`, `refusal` and `image_url` (the OpenAI
content-part types for text, assistant refusals and images). A text-bearing part that carries
anything besides its one content key is replaced whole too.

What is not removed is text. A `text` part is traced as written, by design: a base64 string a
caller types into a text part is traced like any other text. The export-time scrub drops
data-URL payloads and runs of more than 128 characters of standard-alphabet base64 from every
string; a shorter run, a URL-safe run or one broken into short lines, written in text, stays.
