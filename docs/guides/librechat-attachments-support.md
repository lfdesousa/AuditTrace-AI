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
| Images (`image_url` parts) | unchanged: a model started without a vision projector answers an error | unchanged |

## What the error looks like

When you choose **Upload to Provider**, the message reaches the model unchanged
(AuditTrace forwards your message's content parts unchanged). The model's server rejects the file part,
and AuditTrace turns that rejection into a standard OpenAI-style error:

```json
{"error": {"message": "This model can't receive files or images directly in the message (unsupported content part type 'file'). Use 'Upload as Text' instead.",
           "type": "invalid_request_error", "param": "messages", "code": "unsupported_content_type", "status": 400}}
```

LibreChat shows the `message`. Choose **Upload as Text** for the same file and send again.

## Notes

- The error is specific: only the model server's "unsupported content part type" rejection is
  translated. Any other rejected request keeps its previous behaviour.
- The audit record of the failed turn keeps the model server's original text, so the event is
  reconstructible.
