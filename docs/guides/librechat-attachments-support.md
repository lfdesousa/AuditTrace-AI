# Attachments in LibreChat with AuditTrace

LibreChat offers several ways to attach a file to a chat. This page says which of
them work with an AuditTrace deployment today, and what you see when one does not.

| LibreChat offers | On AuditTrace | Status |
|---|---|---|
| **Upload as Text** (the file's text is placed in your message) | works with the deployed model; the model reads the text as part of your message | **supported** |
| **Upload to Provider** (the file travels inside the message as a `file` content part) | the deployed model does not accept file parts; you get an explicit error that names the alternative (below) | **not supported, explicit error** |
| **File Search** (a provider-side vector store) | answered from your per-chat session memory instead; see [per-chat session recall](per-chat-session-recall.md) | supported, rewired |
| OCR on uploaded scans | planned, through the document pipeline | later |
| keeping a provider upload in session memory | planned | later |
| the newer upstream delivery setting (`defaultLLMDeliveryPath`) | arrives with a future upstream sync | later |
| Images (`image_url` parts) | accepted, also from the LibreChat menu (**Upload to Provider** with an image): the deployed model reads the image (it runs with a vision projector); the audit record names each image by its declared type, size and sha256, and never keeps the image | **supported** |

## What the error looks like

When you choose **Upload to Provider**, the message reaches the model unchanged
(AuditTrace forwards your message's content parts unchanged). The model's server rejects the file part,
and AuditTrace turns that rejection into a standard OpenAI-style error:

```json
{"error": {"message": "The model server does not accept one of the content part types in this message (non-text part types sent: 'file'). To attach a document, use 'Upload as Text' instead.",
           "type": "invalid_request_error", "param": "messages", "code": "unsupported_content_type", "status": 400}}
```

LibreChat shows the `message`. Choose **Upload as Text** for the same file and send again.

## Notes

- An image sent to a model whose server has no vision projector gets its own explicit error,
  `image_input_unsupported` (HTTP 400): choose a model that accepts images, or remove the image.
- The errors are specific: only the model server's "unsupported content part type" rejection and
  its "image input is not supported" rejection are translated, each into its own explicit error.
  Any other rejected request keeps its previous behaviour.
- The audit record of the failed turn keeps the model server's original text, so the event is
  reconstructible.
- Every image sent in a message is named in the audit record of that turn (`input_media`): its position,
  the media type the client declared, its size in bytes and the sha256 of its bytes. The image itself is
  not kept, in the record or in traces.
- An image must travel inside the message. A link to an image is refused with `image_url_not_supported`
  (HTTP 400): this service does not fetch images. A damaged inline image is refused with
  `image_data_invalid` (HTTP 400): attach it again. A link in any other media part (audio or video) is
  refused with `media_link_not_supported` (HTTP 400). A refused turn is recorded as a failed turn; the
  link itself is stored nowhere.
- In the audit record, an empty `input_media` means **no image recorded**. Turns from before this
  feature, and rows that are not chat turns, are always empty; see the
  [audit record integrity note](../reference/audittrace/audit-record-integrity.md).
