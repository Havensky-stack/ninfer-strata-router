#!/usr/bin/env python3
"""Small stdio MCP server that exposes Codex's bundled workspace dependency paths.

Codex Desktop normally injects ``load_workspace_dependencies`` as a dynamic tool.
Custom-provider threads can be created without that dynamic tool.  This server gives
those threads the same read-only runtime information from user configuration instead.
"""

import json
import os
import sys
from pathlib import Path


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "workspace-deps"
SERVER_VERSION = "0.1.0"


def dependency_paths():
    runtime = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime"
    deps = runtime / "dependencies"
    node = deps / "node" / "bin" / "node.exe"
    node_modules = deps / "node" / "node_modules"
    python = deps / "python" / "python.exe"
    git = deps / "native" / "git" / "cmd" / "git.exe"
    artifact = node_modules / "@oai" / "artifact-tool"

    lines = ["Workspace dependencies are available.", "", "### Workspace Dependencies"]
    if artifact.is_dir():
        lines.append("- @oai/artifact-tool: installed")
    else:
        lines.append("- @oai/artifact-tool: not found")
    lines.append("- Node.js executable: `%s`" % node)
    lines.append("- Node.js packages: `%s`" % node_modules)
    lines.append("- Python executable: `%s`" % python)
    lines.append("- Git executable: `%s`" % git)
    lines.append("- Override binaries: `%s`" % (deps / "bin" / "override"))
    lines.append("- Fallback binaries: `%s`" % (deps / "bin" / "fallback"))
    lines.append("")
    lines.append("Use the Node.js executable and package directory above as the supported")
    lines.append("runtime for the presentations skill.  Do not guess other package paths.")
    return "\n".join(lines)


TOOL = {
    "name": "load_workspace_dependencies",
    "description": (
        "Locate the configured bundled workspace dependency runtime paths for this "
        "local desktop thread, including Node.js, Python, and libraries for slides, "
        "spreadsheets, documents, and PDFs. This is read-only and takes no arguments."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


def reply(message_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def error(message_id, code, message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id,
                                 "error": {"code": code, "message": message}},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(message):
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        reply(message_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Provides Codex's bundled workspace runtime paths when the usual "
                "codex_app dynamic tool is unavailable."
            ),
        })
    elif method == "tools/list":
        reply(message_id, {"tools": [TOOL]})
    elif method == "tools/call":
        if (params.get("name") or "") != TOOL["name"]:
            error(message_id, -32602, "unknown tool: %s" % params.get("name"))
            return
        reply(message_id, {
            "content": [{"type": "text", "text": dependency_paths()}],
            "isError": False,
        })
    elif method == "resources/list":
        reply(message_id, {"resources": []})
    elif method == "resources/templates/list":
        reply(message_id, {"resourceTemplates": []})
    elif method == "prompts/list":
        reply(message_id, {"prompts": []})
    elif method == "ping":
        reply(message_id, {})
    elif method and method.startswith("notifications/"):
        return
    elif message_id is not None:
        error(message_id, -32601, "method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            handle(message)


if __name__ == "__main__":
    if "--print" in sys.argv:
        print(dependency_paths())
    else:
        main()
