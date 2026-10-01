#!/usr/bin/env python3
"""Browser Buddy native messaging host.

This process is launched by Chrome (see the native messaging host manifest).
It bridges two sides:

  1. Chrome extension  <->  this host : Chrome Native Messaging protocol
     (4-byte little-endian length prefix + UTF-8 JSON) over stdin/stdout.
  2. This host  <->  MCP server : newline-delimited JSON over a Unix domain
     socket. The MCP server is short-lived (one per Claude Code session) and
     connects, sends one request, waits for one response, disconnects.

Message flow for a tool call:
  MCP server --{"id":7,"cmd":"read_active_tab"}\\n--> host
  host --[len][{"id":7,"cmd":"read_active_tab"}]--> extension
  extension --[len][{"id":7,"ok":true,...}]--> host
  host --{"id":7,"ok":true,...}\\n--> MCP server

Stdlib only. No third-party dependencies.
"""

import json
import os
import socket
import struct
import sys
import tempfile
import threading

HOST_NAME = "com.browserbuddy.host"
REQUEST_TIMEOUT_SECS = 90


def socket_path():
    """Location of the Unix socket the MCP server connects to.

    NOTE: keep in sync with pypi/browser_buddy_mcp.py (socket_path).

    Uses $XDG_RUNTIME_DIR when set (per-user, already private on Linux);
    falls back to the shared temp dir. The directory is created with
    mode 0700 so other local users cannot connect to or hijack it.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    d = os.path.join(base, "browser-buddy")
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return os.path.join(d, "browser-buddy.sock")


def read_native_message(stream):
    """Read one length-prefixed message from a binary stream.

    Returns the decoded object, or None on clean EOF.
    """
    raw_len = stream.read(4)
    if len(raw_len) == 0:
        return None
    if len(raw_len) < 4:
        raise IOError("truncated native message length prefix")
    (length,) = struct.unpack("<I", raw_len)
    if length > 64 * 1024 * 1024:
        raise IOError("native message too large: %d bytes" % length)
    data = stream.read(length)
    if len(data) < length:
        raise IOError("truncated native message body")
    return json.loads(data.decode("utf-8"))


def encode_native_message(obj):
    """Encode an object to length-prefixed bytes for Chrome."""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(data)) + data


def write_native_message(stream, obj):
    stream.write(encode_native_message(obj))
    stream.flush()


class Bridge:
    """Routes requests between the MCP server (Unix socket) and the
    Chrome extension (native-messaging stdio)."""

    def __init__(self, send_to_extension, request_timeout=REQUEST_TIMEOUT_SECS):
        # send_to_extension(obj) -> None ; how replies/requests go to Chrome
        self._send = send_to_extension
        self._timeout = request_timeout
        self._lock = threading.Lock()
        self._pending = {}  # req_id -> {"conn": socket, "timer": Timer}
        self._running = True

    # ---- extension side -------------------------------------------------
    def handle_extension_message(self, msg):
        """Process one decoded message arriving from the extension."""
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "hello":
            self._send({"type": "hello_ack", "ok": True})
            return
        req_id = msg.get("id")
        if req_id is None:
            return
        with self._lock:
            entry = self._pending.pop(req_id, None)
        if entry is None:
            return  # stale / timed-out reply; ignore
        entry["timer"].cancel()
        try:
            conn = entry["conn"]
            conn.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                entry["conn"].close()
            except OSError:
                pass

    # ---- MCP-server side ------------------------------------------------
    def handle_mcp_request(self, payload, conn):
        """Process one decoded NDJSON request from the MCP server.

        payload must be a dict with an "id" and a "cmd".
        """
        req_id = payload.get("id") if isinstance(payload, dict) else None
        cmd = payload.get("cmd") if isinstance(payload, dict) else None
        if req_id is None or cmd not in ("read_active_tab", "open_and_read_url"):
            self._reply_error(conn, req_id,
                              "bad request: need {id, cmd: read_active_tab|open_and_read_url}")
            return
        timer = threading.Timer(self._timeout, self._on_timeout, args=(req_id,))
        with self._lock:
            self._pending[req_id] = {"conn": conn, "timer": timer}
        timer.start()
        try:
            self._send(payload)
        except Exception as exc:  # noqa: BLE001 - report transport failure
            with self._lock:
                entry = self._pending.pop(req_id, None)
            if entry is not None:
                entry["timer"].cancel()
            self._reply_error(conn, req_id, "failed to reach extension: %s" % exc)

    def _on_timeout(self, req_id):
        with self._lock:
            entry = self._pending.pop(req_id, None)
        if entry is None:
            return
        self._reply_error(entry["conn"], req_id,
                          "extension did not respond in %ds (is the tab still loading?)" % self._timeout)

    @staticmethod
    def _reply_error(conn, req_id, error):
        try:
            conn.sendall((json.dumps({"id": req_id, "ok": False, "error": error}) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def shutdown(self):
        self._running = False
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for req_id, entry in pending:
            entry["timer"].cancel()
            self._reply_error(entry["conn"], req_id, "host shutting down")


def _extension_reader(bridge, stdin):
    """Thread: pump length-prefixed messages from the extension."""
    try:
        while True:
            msg = read_native_message(stdin)
            if msg is None:  # Chrome closed the port
                break
            try:
                bridge.handle_extension_message(msg)
            except Exception as exc:  # noqa: BLE001 - never kill the pump
                sys.stderr.write("browser-buddy: extension message error: %s\n" % exc)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("browser-buddy: native stdin error: %s\n" % exc)
    finally:
        # Chrome is gone; nothing left to bridge.
        os._exit(0)  # noqa: SLF001 - intentional: kill socket threads too


def _serve_mcp(bridge, sock_path):
    """Accept MCP-server connections; one request per connection."""
    if os.path.exists(sock_path):
        try:
            os.unlink(sock_path)
        except OSError:
            pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(8)

    def handle_conn(conn):
        # Ownership of `conn` passes to the bridge: it stays open until the
        # extension replies (or the request times out), then the bridge closes it.
        try:
            f = conn.makefile("r", encoding="utf-8")
            line = f.readline()
            if not line:
                conn.close()
                return
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                Bridge._reply_error(conn, None, "request must be a single JSON object per line")
                return
            bridge.handle_mcp_request(payload, conn)
        except OSError:
            try:
                conn.close()
            except OSError:
                pass

    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            break
        t = threading.Thread(target=handle_conn, args=(conn,), daemon=True)
        t.start()


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    write_lock = threading.Lock()

    def send_to_extension(obj):
        with write_lock:
            write_native_message(stdout, obj)

    bridge = Bridge(send_to_extension)
    sock = socket_path()

    reader = threading.Thread(target=_extension_reader, args=(bridge, stdin), daemon=True)
    reader.start()
    sys.stderr.write("browser-buddy host: socket at %s\n" % sock)
    _serve_mcp(bridge, sock)


if __name__ == "__main__":
    main()
