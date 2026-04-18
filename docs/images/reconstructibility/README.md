# Reconstructibility walkthrough — screenshot capture guide

These screenshots are embedded in `docs/reconstructibility-walkthrough.md`. The doc is the pitch baseline; the images are what a CTO actually clicks on.

**Capture conventions**
- PNG, 1600×1000 or wider (retina export is fine).
- Dark theme preferred — it matches the architecture colour palette and prints cleanly on dark slides.
- Zoom browser to ≥110% before capture so text stays legible when slides downscale.
- Redact any prod sub claims if the UI shows another user's data (shouldn't happen via the Luis token, but double-check).
- Filename convention: `NN-short-name.png`, `NN` matching the hop number in the doc.

Run this probe first so every screenshot is of the same request:

```bash
BEARER=$(scripts/audittrace-login --show)
curl -sk \
  -H "Authorization: Bearer $BEARER" \
  -H "Content-Type: application/json" \
  -H "X-Project: reconstructibility-demo" \
  -X POST https://audittrace.local:30952/v1/chat/completions \
  -d '{
    "model": "qwen3.6-35b-a3b",
    "stream": false,
    "messages": [{"role": "user", "content":
      "Consult recall_decisions and recall_semantic to answer: what did ADR-027 decide about memory storage? Keep it to two short sentences."
    }]
  }'
```

Note the `response_id` from the output; you'll need the trace it produces.

## Shot list

| # | Filename | Tool | URL / Path | Frame |
|---|---|---|---|---|
| 1 | `01-langfuse-trace-list.png` | Langfuse | `http://localhost:3000/project/<id>/traces` — set the "User" filter to your sub | The list view showing several traces with populated `user` column and non-empty Input/Output counts |
| 2 | `02-langfuse-trace-tree.png` | Langfuse | click the probe trace → Timeline tab | The full observation tree. Expand `sovereign-chat-request` so `llm.chat.completions` + `memory_tool.*` children are visible |
| 3 | `03-langfuse-root-observation.png` | Langfuse | click `POST /v1/chat/completions` observation | The right-hand panel with Input (messages array) + Output (answer) both populated. Proves no more undefined. |
| 4 | `04-langfuse-generation.png` | Langfuse | click `llm.chat.completions` observation | The Generation card with model name, prompt preview, completion, token usage |
| 5 | `05-langfuse-tool-call.png` | Langfuse | click `memory_tool.recall_decisions` observation | Input (args), Output (result summary), `langfuse.user.id` visible in metadata |
| 6 | `06-tempo-service-map.png` | Grafana / Tempo | `http://localhost:3001/d/audittrace-call-flow-tempo` → Service Graph panel (or Explore → Tempo → Service Graph) | The node graph with all 8 edges radiating from `audittrace-server`. Hover one edge to show the latency histogram before capture if it looks good |
| 7 | `07-tempo-flamegraph.png` | Grafana / Tempo | Explore → Tempo → paste the probe's trace_id | The flamegraph / waterfall view of the 74 spans. **Already captured 2026-04-18** — shows the full call chain with `llm.chat.completions` (1m 3s) dominating every other span (all sub-millisecond). Pitch-grade framing already. |
| 8 | `08-grafana-sovereign-ops.png` | Grafana | `http://localhost:3001/d/sovereign-overview` | The whole Sovereign AI Operations dashboard with all panels populated (Queue Saturation, Throughput, Container Logs, Latency) |

## If a shot is hard to frame

- **Service map too sparse** — fire 3-5 probes quickly before capturing so the metrics-generator has populated every edge. The `sum(rate(traces_service_graph_request_total[5m]))` needs at least a sample per edge in the last 5 min.
- **Langfuse tree collapsed** — click the arrow on every row to expand. Use the "Timeline" tab view, not "Preview", for the tree shot.
- **Generation card no completion** — some probes return tool_calls on the first iteration and text on later ones. Pick the LAST `llm.chat.completions` child in the tree for the Generation shot; that's the one with the final text completion.

## After capturing

```bash
# From the repo root
cp ~/Downloads/NN-*.png docs/images/reconstructibility/
git add docs/images/reconstructibility/*.png
git commit -m "docs(images): reconstructibility walkthrough screenshots"
```

The doc already references these paths; no edit needed to `reconstructibility-walkthrough.md` after dropping the files in.
