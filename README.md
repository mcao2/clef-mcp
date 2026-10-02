# clef-mcp

MCP server exposing Cloudflare Workers AI [clef](https://developers.cloudflare.com/workers-ai/models/clef/) decision models (`clef`, `clef-flash`). Single file, Python 3 stdlib, no dependencies.

Clef takes a `state` and a schema of typed `questions`, and returns a probability for every allowed option of every question — for classification, triage, gating, labeling, and quality judgments instead of a chat model.

## Tool

```
clef_decide(state, questions, model?)
```

Question types (1–64 per call; ids: letters, digits, `_`, `.`, `-`, max 100 chars; batch all into one call):

| Type | Required | Optional | Returns |
|------|----------|----------|---------|
| `noul` | `instructions` | `criteria: {"true": "…", "false": "…"}` | `noul` = P(yes) |
| `choice` | `instructions`, `criteria: {option: description}` (2–255 options) | — | `choice`, `probabilities`, `confidence` |
| `score` | `instructions`, `criteria: [lowest … highest]` (2–10 levels) | — | `score` (probability-weighted index), `probabilities`, `confidence` |

`state` may be a string or structured data (records, chat logs, app state); long text is truncated to the model's token limit (65,536). Optional `images` (1–4, PNG/JPEG/WebP as data URLs or `{content_type, base64}`) are placed before the state for vision evaluation.

```json
{"state": "Checkout failing for every customer for the last hour.",
 "questions": {
   "urgent": {"type": "noul", "instructions": "Is this urgent?"},
   "team": {"type": "choice", "instructions": "Which team?",
            "criteria": {"billing": "Payments", "technical": "Outages"}}}}
```

→ `{"answers": {"urgent": {"noul": 0.99}, "team": {"choice": "technical", "confidence": 0.58}}, "usage": {"input_tokens": 223}}`

## Endpoint configuration

No config file — the endpoint is selected by environment variables at startup:

| Env var | Mode | Meaning |
|---------|------|---------|
| `CLEF_BASE_URL` | proxy | URL to POST `{model, state, questions}` to, e.g. `http://localhost:8317/v1/ai/run` |
| `CLEF_API_KEY` | proxy | Bearer key for that URL |
| `CLOUDFLARE_ACCOUNT_ID` | direct | Cloudflare account ID (wrangler-standard name) |
| `CLOUDFLARE_API_TOKEN` | direct | Cloudflare API token (wrangler-standard name) |

- **`CLEF_BASE_URL` set → proxy mode.** One fixed endpoint; `model` is resolved server-side by the proxy.
- **`CLEF_BASE_URL` unset → direct mode.** URL built per call: `https://api.cloudflare.com/client/v4/accounts/<CLOUDFLARE_ACCOUNT_ID>/ai/run/@cf/cloudflare/<model>`; body `model` must be the bare name (`clef` | `clef-flash`).
- Direct mode: use a least-privilege API token with only the **Workers AI** permission.

Both modes send the same body and return the same envelope; the server unwraps `{result: {answers, usage}}`. The active route is embedded in the tool description at runtime, so agents always see where requests go.

## Registering

Generic `mcpServers` JSON — Pi, Claude Code, Cursor, VS Code, Windsurf, Cline:

```json
{"mcpServers": {"clef": {
  "command": "python3",
  "args": ["/path/to/clef-mcp/clef-mcp.py"],
  "env": {"CLEF_BASE_URL": "http://localhost:8317/v1/ai/run",
          "CLEF_API_KEY": "<key>"}}}}
```

Claude Code CLI equivalent:

```sh
claude mcp add clef --scope user \
  --env CLEF_BASE_URL=http://localhost:8317/v1/ai/run \
  --env CLEF_API_KEY=<key> \
  -- python3 /path/to/clef-mcp/clef-mcp.py
```

Codex CLI (`~/.codex/config.toml`):

```toml
[mcp_servers.clef]
command = "python3"
args = ["/path/to/clef-mcp/clef-mcp.py"]

[mcp_servers.clef.env]
CLEF_BASE_URL = "http://localhost:8317/v1/ai/run"
CLEF_API_KEY = "<key>"
```

OpenCode (`opencode.json`): same fields under `mcp.clef`, with `command` as an array and `environment` instead of `env`.

Direct mode in any harness: drop the `CLEF_*` vars, set `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`.

## Test

```sh
python3 smoke_test.py   # handshake, metadata, error codes, batch, live call
```

Spec notes: MCP stdio (2024-11-05 / 2025-06-18), tool annotations (`readOnly`, `idempotent`, `openWorld`), `outputSchema` + `structuredContent` with text fallback, unknown tool → `-32602`, JSON-RPC batching. Diagnostics to stderr only; `CLEF_MCP_DEBUG=1` for logging; `CLEF_HTTP_TIMEOUT` (default 30s).
