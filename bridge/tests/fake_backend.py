"""Synthetic stdio peer: never imports a real mail service."""
import json
import os
import sys
import time

DEFINITION = {"name": "mail_synthetic", "title": "Synthetic title",
              "description": "Synthetic only", "inputSchema": {"type": "object"},
              "annotations": {"readOnlyHint": False, "destructiveHint": True,
                              "idempotentHint": False, "openWorldHint": True},
              "_meta": {"test": "kept"}}
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "synthetic", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [DEFINITION], "_meta": {"listing": "kept"}}
    elif method == "tools/call":
        arguments = request["params"].get("arguments") or {}
        if arguments.get("marker_file"):
            with open(arguments["marker_file"], "a") as stream:
                stream.write("one dispatch\n")
        if arguments.get("crash"):
            sys.stderr.write("PRIVATE_BACKEND_SECRET\n")
            os._exit(7)
        if arguments.get("delay"):
            time.sleep(arguments["delay"])
        result = {"content": [{"type": "text", "text": "synthetic private result"}],
                  "isError": arguments.get("is_error", False),
                  "structuredContent": {"preserved": True}, "_meta": {"private": "kept"}}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
