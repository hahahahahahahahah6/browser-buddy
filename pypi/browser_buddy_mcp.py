#!/usr/bin/env python3
"""Browser Buddy MCP server (stdlib only, no `mcp` package).

Speaks JSON-RPC 2.0 over stdio using newline-delimited JSON (the framing
MCP stdio servers use) and forwards tool calls to the Browser Buddy native
host over its Unix socket.

Tools:
  read_active_tab  - extract readable text from the user's active Chrome tab
  open_and_read_url - open a URL in a background tab (real profile: cookies,
                     logins, sessions), extract readable text, close the tab

Stdout is reserved for protocol messages; logs go to stderr.
"""

import json
import os
import socket
import sys
import tempfile
import urllib.parse

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "browser-buddy"
SERVER_VERSION = "0.1.0"
DEFAULT_TIMEOUT_SECS = 75


def socket_path():
    """Must match host/browser_buddy_host.py.

    Uses $XDG_RUNTIME_DIR when set (per-user, already private on Linux);
    falls back to the shared temp dir.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "browser-buddy", "browser-buddy.sock")


def config_path():
    return os.path.join(os.path.expanduser("~"), ".config",
                        "browser-buddy", "config.json")


_config_cache = None


def load_config():
    """Read ~/.config/browser-buddy/config.json. Never raises."""
    global _config_cache
    if _config_cache is None:
        cfg = {}
        try:
            with open(config_path(), "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                cfg = loaded
        except (OSError, ValueError):
            pass
        _config_cache = cfg
    return _config_cache


_allowed_warned = False


def url_allowed(url):
    """Domain allowlist for open_and_read_url.

    `allowed_domains` in the config lists domains the agent may open
    (entries match the domain itself and its subdomains). When the list is
    empty or missing, everything is allowed but a one-time warning is
    logged: without a list, a prompt-injected page could steer the agent
    to open arbitrary URLs in your logged-in browser.
    """
    global _allowed_warned
    allowed = load_config().get("allowed_domains") or []
    if not allowed:
        if not _allowed_warned:
            _allowed_warned = True
            log("warning: no allowed_domains set in %s; open_and_read_url "
                "accepts any URL. Set allowed_domains to restrict which "
                "sites the agent may open in your browser." % config_path())
        return True
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d.lower() or host.endswith("." + d.lower())
               for d in allowed if isinstance(d, str) and d)


def log(msg):
    sys.stderr.write("browser-buddy-mcp: %s\n" % msg)
    sys.stderr.flush()


TOOLS = [
    {
        "name": "read_active_tab",
        "description": (
            "Read the user's currently active Chrome tab through their real, "
            "logged-in browser profile. Returns the page URL, title, and "
            "cleaned readable text. Use when WebFetch/fetch is blocked by a "
            "403, login wall, or bot detection."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "open_and_read_url",
        "description": (
            "Open a URL in a background Chrome tab using the user's real "
            "browser profile (cookies and login sessions included), extract "
            "its readable text, then close the tab. For pages behind logins, "
            "paywalls-with-session, or bot checks that defeat server-side "
            "fetchers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s) URL to open and read"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
]


def call_host(cmd, args=None, timeout=DEFAULT_TIMEOUT_SECS):
    """Send one request to the native host; return its decoded response dict."""
    payload = {"id": 1, "cmd": cmd}
    if args:
        payload.update(args)
    path = socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(path)
    except OSError:
        s.close()
        return {
            "id": 1,
            "ok": False,
            "error": (
                "browser-buddy host is not reachable at %s. "
                "Is Chrome running with the Browser Buddy extension loaded? "
                "See README.md installation steps." % path
            ),
        }
    try:
        with s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            f = s.makefile("r", encoding="utf-8")
            line = f.readline()
            if not line:
                return {"id": 1, "ok": False, "error": "host closed the connection"}
            return json.loads(line)
    except (OSError, json.JSONDecodeError) as exc:
        return {"id": 1, "ok": False, "error": "host communication failed: %s" % exc}


def tool_result_text(text, is_error=False):
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def format_page_result(resp):
    header = "URL: %s\nTitle: %s%s\n\n" % (
        resp.get("url", ""),
        resp.get("title", ""),
        " (truncated)" if resp.get("truncated") else "",
    )
    return header + resp.get("text", "")


def handle_tools_call(params):
    name = (params or {}).get("name")
    args = (params or {}).get("arguments") or {}
    if name == "read_active_tab":
        resp = call_host("read_active_tab")
    elif name == "open_and_read_url":
        url = args.get("url", "")
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            return tool_result_text("error: 'url' must be an http(s) URL", is_error=True)
        if not url_allowed(url):
            return tool_result_text(
                "error: domain not in allowed_domains (see %s). "
                "Refusing to open this URL in your logged-in browser."
                % config_path(), is_error=True)
        resp = call_host("open_and_read_url", {"url": url})
    else:
        return tool_result_text("error: unknown tool '%s'" % name, is_error=True)
    if not resp.get("ok"):
        return tool_result_text("error: %s" % resp.get("error", "unknown"), is_error=True)
    return tool_result_text(format_page_result(resp))


def handle_rpc(request):
    """Handle one JSON-RPC request object; return the response object
    (or None for notifications, which get no response)."""
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request"}}
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}

    def result(res):
        return {"jsonrpc": "2.0", "id": req_id, "result": res}

    def error(code, message):
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        return result({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method == "tools/call":
        return result(handle_tools_call(params))
    return error(-32601, "method not found: %s" % method)


def serve(stdin, stdout):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        try:
            response = handle_rpc(request)
        except Exception as exc:  # noqa: BLE001 - never break the stdio loop
            log("handler error: %s" % exc)
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32603, "message": "internal error"}}
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main():
    serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
