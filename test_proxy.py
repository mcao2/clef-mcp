#!/usr/bin/env python3
"""Offline checks for proxy routing, authentication, and response formats."""
import json
import os
from pathlib import Path
import runpy
from unittest.mock import patch


def main():
    server = runpy.run_path(str(Path(__file__).with_name("clef-mcp.py")))
    base = "https://proxy.example/run/{model}"
    questions = {"ok": {"type": "noul", "instructions": "All good?"}}
    answer = {"answers": {"ok": {"noul": 0.99}}, "usage": {"input_tokens": 20}}
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "clef_decide", "arguments": {
                   "state": "Healthy", "questions": questions}}}

    with patch.dict(os.environ, {"CLEF_BASE_URL": base}, clear=True):
        with patch("urllib.request.urlopen") as fetch:
            for model, payload in [("clef", answer),
                                   ("clef-flash", {"success": True, "result": answer})]:
                request["params"]["arguments"]["model"] = model
                fetch.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                response = server["process_message"](request)["result"]
                assert not response["isError"], response
                assert response["structuredContent"] == answer, response
                upstream = fetch.call_args.args[0]
                assert upstream.full_url == base.replace("{model}", model)
                assert upstream.get_header("Authorization") is None
                assert json.loads(upstream.data) == {
                    "model": model, "state": "Healthy", "questions": questions}

            fetch.reset_mock()
            request["params"]["arguments"]["model"] = "../../other-endpoint"
            assert server["process_message"](request)["result"]["isError"]
            assert not fetch.called, "Invalid models must not reach upstream"

    with patch.dict(os.environ, {"CLEF_BASE_URL": "https://proxy.example/ai/run",
                                "CLEF_API_KEY": "dummy-proxy-key"}, clear=True):
        with patch("urllib.request.urlopen") as fetch:
            request["params"]["arguments"]["model"] = "custom-alias"
            fetch.return_value.__enter__.return_value.read.return_value = json.dumps(
                {"result": answer}).encode()
            response = server["process_message"](request)["result"]
            assert not response["isError"], response
            assert response["structuredContent"] == answer, response
            upstream = fetch.call_args.args[0]
            assert upstream.full_url == "https://proxy.example/ai/run"
            assert upstream.get_header("Authorization") == "Bearer dummy-proxy-key"
            assert json.loads(upstream.data)["model"] == "custom-alias"

    with patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "dummy-account"}, clear=True):
        try:
            server["build_request"]("clef", "Healthy", questions)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Direct mode must still require a Cloudflare token")

    with patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "dummy-account",
                                "CLOUDFLARE_API_TOKEN": "dummy-token"}, clear=True):
        upstream = server["build_request"]("clef-flash", "Healthy", questions)
        assert upstream.full_url == (
            "https://api.cloudflare.com/client/v4/accounts/dummy-account/"
            "ai/run/@cf/cloudflare/clef-flash")
        assert upstream.get_header("Authorization") == "Bearer dummy-token"

    print("proxy checks passed (offline)")


if __name__ == "__main__":
    main()
