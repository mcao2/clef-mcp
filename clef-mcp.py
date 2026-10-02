#!/usr/bin/env python3
"""Clef Decision MCP server — Cloudflare's clef decision models over stdio.

Tool: clef_decide(state, questions, model?) -> probability-weighted answers.

Routing (env-selected):
- Cloudflare Workers AI via a proxy endpoint (e.g. /v1/ai/run):
    CLEF_BASE_URL=http://localhost:8317/v1/ai/run
    CLEF_API_KEY=<proxy api key>             (optional if proxy handles auth)
  Body {model, state, questions} is forwarded as-is. The URL may contain
  {model}, replaced with clef or clef-flash for each call.
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

QUESTION_DOC = """Schema (official Cloudflare contract): questions maps a
question id (letters, digits, '_', '.', '-', max 100 chars) to a typed
question. 1 to 64 questions per call; answers are returned under the
same ids. "instructions" is a non-empty string, or an object/array
holding the question in one field and referenced data in others.
- type "noul" (yes/no): required "instructions"; optional "criteria":
  {"true": "what a yes means", "false": "what a no means"}.
  -> answers.<id>.noul = P(yes)
- type "choice" (classification): required "instructions" and
  "criteria": {option: description} with 2-255 options (description may
  be string, object, array, or null).
  -> answers.<id>.choice = picked option, .probabilities = per option,
     .confidence
- type "score" (ordinal rating): required "instructions" and
  "criteria": ordered array of 2-10 level descriptions, lowest first
  (levels indexed from 0).
  -> answers.<id>.score = probability-weighted index, .probabilities
     per level, .confidence
Batch all questions about one state into a single call. Example:
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


def build_request(model, state, questions, images=None):
    mode, base, key = routing()
    if mode == "custom":
        if "{model}" in base and model not in ("clef", "clef-flash"):
            raise RuntimeError("model must be clef or clef-flash")
        url = base.replace("{model}", model)
    else:
        if not base:
            raise RuntimeError("direct mode requires CLOUDFLARE_ACCOUNT_ID")
        url = ("https://api.cloudflare.com/client/v4/accounts/"
               f"{base}/ai/run/@cf/cloudflare/{model}")
    if not key and mode == "cloudflare":
        raise RuntimeError("direct mode requires CLOUDFLARE_API_TOKEN")
    body = {"model": model, "state": state, "questions": questions}
    if images:
        body["images"] = images
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    return req


def routing_note():
    mode, base, _ = routing()
    if mode == "custom":
        return f"Cloudflare Workers AI (via {base})"
    if base:
        return f"Cloudflare Workers AI (account {base}, direct API)"
    return ("MISCONFIGURED: set CLEF_BASE_URL (proxy mode), "
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
                    "description": "The situation to decide on: any text, or "
                                   "structured data (object/array) such as "
                                   "records, chat logs, or application state. "
                                   "Long text is truncated to the model's "
                                   "token limit (65,536).",
                },
                "questions": {
                    "type": "object",
                    "minProperties": 1,
                    "maxProperties": 64,
                    "description": QUESTION_DOC,
                },
                "model": {
                    "type": "string",
                    "enum": ["clef", "clef-flash"],
                    "description": "clef (default) or clef-flash (faster/"
                                   "cheaper, for high-volume low-stakes "
                                   "decisions).",
                },
                "images": {
                    "type": "array",
                    "maxItems": 4,
                    "description": "Optional images placed before the state "
                                   "for vision evaluation: PNG, JPEG, or "
                                   "WebP, each a data URL (data:image/png;"
                                   "base64,...) or {content_type, base64}. "
                                   "Max 4 MiB and 16 megapixels per image, "
                                   "8 MiB total decoded, 13 MiB request "
                                   "body. Remote URLs are not accepted.",
                    "items": {"anyOf": [
                        {"type": "string",
                         "pattern": "^[Dd][Aa][Tt][Aa]:"},
                        {"type": "object",
                         "properties": {"content_type": {"type": "string"},
                                         "base64": {"type": "string"}},
                         "required": ["content_type", "base64"]},
                    ]},
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
    images = args.get("images")
    model = args.get("model") or DEFAULT_MODEL
    if not isinstance(model, str) or not model.strip():
        model = DEFAULT_MODEL
    if state is None or state == "" or state == {} or state == []:
        return tool_result(rid, "state must be non-empty", is_error=True)
    if not isinstance(questions, dict) or not questions:
        return tool_result(rid, "questions must be a non-empty object",
                           is_error=True)
    if len(questions) > 64:
        return tool_result(rid, "questions accepts at most 64 items",
                           is_error=True)
    if images is not None:
        if not isinstance(images, list) or not images:
            return tool_result(rid, "images must be a non-empty array when "
                               "provided", is_error=True)
        if len(images) > 4:
            return tool_result(rid, "images accepts at most 4 items",
                               is_error=True)
    try:
        req = build_request(model, state, questions, images)
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
    result = payload.get("result") or payload
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
