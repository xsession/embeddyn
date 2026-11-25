"""Minimal MCP server over stdio (JSON-RPC 2.0, one message per line), standard library only.

Implements initialize, tools/list, tools/call and ping. Tool results are JSON text, capped in
size so that small models are not flooded; long data goes to files whose paths are returned.
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

from . import tools

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "mcu-emu", "version": "1.0.0"}
MAX_RESULT = 12000          # characters of JSON returned per call


def _log(message: str) -> None:
    print(f"[mcu-emu] {message}", file=sys.stderr, flush=True)


def _result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _call_tool(name: str, arguments: dict) -> dict:
    entry = tools.REGISTRY.get(name)
    if entry is None:
        return {"content": [{"type": "text", "text": json.dumps({"ok": False, "error": f"unknown tool {name}"})}],
                "isError": True}
    func: Callable[..., Any] = entry["func"]
    try:
        value = func(**(arguments or {}))
        is_error = isinstance(value, dict) and value.get("ok") is False
    except TypeError as exc:                       # wrong or missing arguments: say which
        value, is_error = {"ok": False, "error": f"bad arguments: {exc}",
                           "expected": list(entry["schema"]["properties"].keys()),
                           "required": entry["schema"].get("required", [])}, True
    except tools.ToolError as exc:
        value, is_error = {"ok": False, "error": str(exc)}, True
    except Exception as exc:                       # noqa: BLE001 - report, never crash the server
        _log(traceback.format_exc())
        value, is_error = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, True
    text = json.dumps(value, indent=1, default=str)
    if len(text) > MAX_RESULT:
        text = text[:MAX_RESULT] + f'\n... [{len(text) - MAX_RESULT} more characters cut; ask for less or use the files]'
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    id_ = message.get("id")
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION)
        return _result(id_, {"protocolVersion": requested, "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": SERVER_INFO,
                             "instructions": "Deterministic MCU-emulation tools. Start with mcu_profile_show; every tool "
                                             "returns JSON with ok=true/false. Read the embedded-emulation skills for the workflow."})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": [{"name": n, "description": e["description"], "inputSchema": e["schema"]}
                                       for n, e in tools.REGISTRY.items()]})
    if method == "tools/call":
        params = message.get("params") or {}
        return _result(id_, _call_tool(params.get("name", ""), params.get("arguments") or {}))
    if id_ is None:
        return None
    return _error(id_, -32601, f"method not found: {method}")


def main() -> None:
    _log(f"started with {len(tools.REGISTRY)} tools")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            reply = _error(None, -32700, f"parse error: {exc}")
        else:
            reply = handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
