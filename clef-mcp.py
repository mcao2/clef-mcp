#!/usr/bin/env python3
"""Clef Decision MCP server — Cloudflare's clef decision models over stdio.

Tool: clef_decide(state, questions, model?) -> probability-weighted answers.

Routing (env-selected):
- Cloudflare Workers AI via a proxy endpoint (e.g. /v1/ai/run):
    CLEF_BASE_URL=http://localhost:8317/v1/ai/run
    CLEF_API_KEY=<proxy api key>
  Body {model, state, questions} is forwarded as-is; "model" is the
  provider-configured alias.
- Cloudflare Workers AI direct API (when CLEF_BASE_URL is unset):
    CLOUDFLARE_ACCOUNT_ID=<account id>       (wrangler-standard naming)
    CLOUDFLARE_API_TOKEN=<cf api token>       (wrangler-standard naming;
                                               least-privilege Workers AI
                                               token recommended)
  Calls POST /client/v4/accounts/<acct>/ai/run/@cf/cloudflare/<model>.

Protocol: MCP over stdio (newline-delimited JSON-RPC 2.0). Compatible with
the 2024-11-05 and 2025-06-18 protocol versions: supports request batching,
tool annotations, structured content, and spec-correct error codes.
Diagnostics go to stderr (never stdout); set CLEF_MCP_DEBUG=1 for logging.
"""
import json
import os
import sys
import urllib.error
import urllib.request

SERVER_NAME = "clef"
SERVER_TITLE = "Clef Decision MCP"
SERVER_VERSION = "1.1.0"
DEFAULT_MODEL = "clef"
HTTP_TIMEOUT = int(os.environ.get("CLEF_HTTP_TIMEOUT", "30"))

QUESTION_DOC = """Schema: questions maps a question name to its definition.
- type "noul": yes/no probability. -> answers.<name>.noul = P(yes)
- type "choice": classification. Add "criteria": {label: description}.
  -> answers.<name>.choice = picked label, .probabilities = per-label, .confidence
- type "score": ordinal rating. Add "criteria": [lowest ... highest].
  -> answers.<name>.score = probability-weighted index (0 = lowest)
Every question needs "instructions". Batch all questions for one state into
a single call. Example:
{"urgent": {"type": "noul", "instructions": "Is this request urgent?"},
 "team": {"type": "choice", "instructions": "Which team handles this?",
          "criteria": {"billing": "Payments", "technical": "Outages"}}}"""


def debug(message):
    if os.environ.get("CLEF_MCP_DEBUG"):
        sys.stderr.write(f"[clef-mcp] {message}\n")
        sys.stderr.flush()


def routing():
    """Return (mode, base, key) describing where requests are sent."""
    base = os.environ.get("CLEF_BASE_URL", "").strip()
    if base:
        return "custom", base, os.environ.get("CLEF_API_KEY", "").strip()
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    return "cloudflare", account, token


def build_request(model, state, questions):
    mode, base, key = routing()
    if mode == "custom":
        url = base
    else:
        if not base:
            raise RuntimeError("direct mode requires CLOUDFLARE_ACCOUNT_ID")
        url = ("https://api.cloudflare.com/client/v4/accounts/"
               f"{base}/ai/run/@cf/cloudflare/{model}")
    if not key:
        raise RuntimeError("missing API key (CLEF_API_KEY for proxy mode, "
                           "CLOUDFLARE_API_TOKEN for direct mode)")
    body = json.dumps({"model": model, "state": state,
                       "questions": questions}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    return req


def routing_note():
    mode, base, _ = routing()
    if mode == "custom":
        return f"Cloudflare Workers AI (via {base})"
    if base:
        return f"Cloudflare Workers AI (account {base}, direct API)"
    return ("MISCONFIGURED: set CLEF_BASE_URL + CLEF_API_KEY (proxy mode), "
            "or CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN (direct mode)")


def tool_def():
    return {
        "name": "clef_decide",
        "title": "Clef decide",
        "description": (
            "Ask the Cloudflare Workers AI clef decision model: give it a "
            "state (any text or JSON describing the situation) and a schema "
            "of typed questions; it returns a probability for every allowed "
            "option of every question. Use for classification, triage, "
            "gating, labeling, and quality judgments instead of a chat model "
            "when you need structured, cheap, deterministic decisions. "
            f"Routing: {routing_note()}. " + QUESTION_DOC),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": ["string", "object", "array"],
                    "description": "The situation to decide on: any text, "
                                   "JSON, or multimodal content parts. "
                                   "Include all relevant context.",
                },
                "questions": {
                    "type": "object",
                    "description": QUESTION_DOC,
                },
                "model": {
                    "type": "string",
                    "description": "clef (default) or clef-flash (faster/"
                                   "cheaper, for high-volume low-stakes "
                                   "decisions).",
                },
            },
            "required": ["state", "questions"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "answers": {
                    "type": "object",
                    "description": "Question name -> answer object "
                                   "(noul / choice / score fields).",
                },
                "usage": {
                    "type": "object",
                    "description": "Upstream token usage.",
                },
            },
            "required": ["answers"],
        },
        "annotations": {
            "title": "Clef decide",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    }


def rpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def tool_result(rid, text, structured=None, is_error=False):
    result = {"content": [{"type": "text", "text": text}],
              "isError": is_error}
    if structured is not None and not is_error:
        result["structuredContent"] = structured
    return rpc_result(rid, result)


def handle_call(rid, params):
    name = params.get("name")
    if name != "clef_decide":
        # Spec: unknown tool is a JSON-RPC Invalid params error.
        return rpc_error(rid, -32602, f"unknown tool: {name}")
    args = params.get("arguments")
    if isinstance(args, str):  # tolerate stringified arguments
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = None
    if not isinstance(args, dict):
        args = {}
    state = args.get("state")
    questions = args.get("questions")
    model = args.get("model") or DEFAULT_MODEL
    if not isinstance(model, str) or not model.strip():
        model = DEFAULT_MODEL
    if state is None or state == "" or state == {} or state == []:
        return tool_result(rid, "state must be non-empty", is_error=True)
    if not isinstance(questions, dict) or not questions:
        return tool_result(rid, "questions must be a non-empty object",
                           is_error=True)
    try:
        req = build_request(model, state, questions)
    except RuntimeError as exc:
        return tool_result(rid, f"config error: {exc}", is_error=True)
    debug(f"call model={model} questions={sorted(questions)}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "replace")
        debug(f"upstream HTTP {exc.code}")
        return tool_result(rid, f"upstream HTTP {exc.code}: {detail}",
                           is_error=True)
    except Exception as exc:  # network/timeout/parse
        debug(f"upstream failure: {exc}")
        return tool_result(rid, f"upstream request failed: {exc}",
                           is_error=True)
    if payload.get("success") is False or payload.get("errors"):
        return tool_result(rid, json.dumps({"errors": payload.get("errors")}),
                           is_error=True)
    result = payload.get("result") or {}
    out = {"answers": result.get("answers"),
           "usage": result.get("usage", {})}
    return tool_result(rid, json.dumps(out), structured=out)


def process_message(req):
    """Return the JSON-RPC response for one request, or None (notification)."""
    rid = req.get("id")
    if rid is None:  # notifications/initialized, notifications/cancelled...
        return None
    method = req.get("method", "")
    if method == "initialize":
        version = (req.get("params") or {}).get("protocolVersion") \
            or "2024-11-05"
        return rpc_result(rid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": SERVER_TITLE,
                           "version": SERVER_VERSION},
            "instructions": (
                "Use clef_decide for structured decisions: classification, "
                "triage, gating, labeling, quality judgment. Prefer it over "
                "chat models when you need probabilities, not prose."),
        })
    if method == "tools/list":
        return rpc_result(rid, {"tools": [tool_def()]})
    if method == "tools/call":
        return handle_call(rid, req.get("params") or {})
    if method == "ping":
        return rpc_result(rid, {})
    return rpc_error(rid, -32601, f"method not found: {method}")


def emit(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            debug(f"invalid JSON line: {exc}")
            continue
        if isinstance(msg, list):  # JSON-RPC batch (2024-11-05 clients)
            replies = [r for r in (process_message(m) for m in msg)
                       if r is not None]
            if replies:
                emit(replies)
        else:
            reply = process_message(msg)
            if reply is not None:
                emit(reply)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:  # client went away
        pass
