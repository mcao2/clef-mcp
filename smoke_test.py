#!/usr/bin/env python3
"""Stdio smoke test for the clef MCP server.

Drives the server over stdio and asserts MCP handshake, tool metadata
(annotations, outputSchema), a live tools/call (when routing is configured),
batch support, and spec-correct error codes.

Usage:
    python3 smoke_test.py                # uses ambient env for routing
    CLEF_BASE_URL=... CLEF_API_KEY=... python3 smoke_test.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "clef-mcp.py")

failures = []


def check(name, condition, detail=""):
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def spawn(env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.Popen([sys.executable, SERVER],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=e)


def send(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def recv(proc):
    return json.loads(proc.stdout.readline())


def main():
    proc = spawn()
    try:
        print("handshake:")
        send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}})
        init = recv(proc)
        check("initialize returns protocolVersion",
              init["result"]["protocolVersion"] == "2025-06-18")
        check("capabilities.tools declared",
              "tools" in init["result"]["capabilities"])
        check("serverInfo has name/title/version",
              init["result"]["serverInfo"].get("title") == "Clef Decision MCP")
        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        print("tools/list:")
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv(proc)["result"]["tools"]
        tool = tools[0]
        check("one tool named clef_decide", tool["name"] == "clef_decide")
        check("annotations present",
              tool.get("annotations", {}).get("readOnlyHint") is True,
              str(tool.get("annotations")))
        check("outputSchema present", "answers" in tool.get("outputSchema", {}).get("required", []))
        check("title present", tool.get("title") == "Clef decide")

        print("error contract:")
        send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "nope", "arguments": {}}})
        err = recv(proc)
        check("unknown tool -> -32602",
              err.get("error", {}).get("code") == -32602, str(err))

        print("batch (two pings):")
        send(proc, [{"jsonrpc": "2.0", "id": 4, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 5, "method": "ping"}])
        batch = recv(proc)
        check("batch replies as array in order",
              isinstance(batch, list) and [b["id"] for b in batch] == [4, 5])

        print("live call:")
        mode = os.environ.get("CLEF_BASE_URL") or "direct"
        send(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "clef_decide", "arguments": {
                        "model": "clef-flash",
                        "state": "Smoke test: the service is healthy.",
                        "questions": {"ok": {"type": "noul",
                                             "instructions": "All good?"}}}}})
        call = recv(proc)["result"]
        check("call succeeds (routing: %s)" % mode, call.get("isError") is False,
              call.get("content", [{}])[0].get("text", ""))
        check("structuredContent present", "answers" in call.get("structuredContent", {}))
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
