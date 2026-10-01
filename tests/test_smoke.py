#!/usr/bin/env python3
"""Smoke tests for Browser Buddy. Stdlib only (unittest).

Covers everything that can run without a real Chrome:
  - Chrome Native Messaging framing (length prefix + JSON)
  - Host Bridge: hello handshake, request routing, timeouts, bad requests
  - MCP server: initialize / tools/list / tools/call (with a stubbed host),
    plus the real socket path's unreachable-host error
"""

import importlib.util
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


host_mod = load_module("browser_buddy_host",
                       os.path.join(REPO_ROOT, "host", "browser_buddy_host.py"))
mcp_mod = load_module("browser_buddy_mcp",
                      os.path.join(REPO_ROOT, "pypi", "browser_buddy_mcp.py"))


class TestNativeFraming(unittest.TestCase):
    def test_roundtrip(self):
        buf = io.BytesIO()
        obj = {"id": 7, "cmd": "read_active_tab", "url": "https://example.com/ü"}
        host_mod.write_native_message(buf, obj)
        raw = buf.getvalue()
        (length,) = struct.unpack("<I", raw[:4])
        self.assertEqual(length, len(raw) - 4)
        buf.seek(0)
        self.assertEqual(host_mod.read_native_message(buf), obj)

    def test_eof_returns_none(self):
        self.assertIsNone(host_mod.read_native_message(io.BytesIO(b"")))

    def test_truncated_prefix_raises(self):
        with self.assertRaises(IOError):
            host_mod.read_native_message(io.BytesIO(b"\x05\x00"))


class FakeExtension:
    """Stands in for the Chrome extension in Bridge tests."""

    def __init__(self, bridge=None, reply=None, reply_delay=0.0):
        self.sent = []          # messages the bridge sent "to Chrome"
        self.bridge = bridge
        self.reply = reply      # dict (without id) to echo back, or None
        self.reply_delay = reply_delay

    def __call__(self, obj):
        self.sent.append(obj)

        def answer():
            if self.reply_delay:
                import time
                time.sleep(self.reply_delay)
            resp = {"id": obj.get("id"), "ok": True}
            resp.update(self.reply or {})
            self.bridge.handle_extension_message(resp)

        if self.reply is not None and "id" in obj:
            t = threading.Thread(target=answer, daemon=True)
            t.start()
            t.join(timeout=5)


def read_json_line(sock, timeout=5):
    sock.settimeout(timeout)
    f = sock.makefile("r", encoding="utf-8")
    line = f.readline()
    return json.loads(line) if line else None


class TestBridge(unittest.TestCase):
    def test_hello_handshake(self):
        sent = []
        bridge = host_mod.Bridge(sent.append)
        bridge.handle_extension_message({"type": "hello"})
        self.assertEqual(sent, [{"type": "hello_ack", "ok": True}])

    def test_request_routed_to_mcp_client(self):
        bridge = host_mod.Bridge(lambda obj: None)
        fake = FakeExtension(reply={"url": "https://example.com",
                                    "title": "Example",
                                    "text": "hello world",
                                    "truncated": False})
        bridge._send = fake
        fake.bridge = bridge

        a, b = socket.socketpair()
        try:
            bridge.handle_mcp_request({"id": 42, "cmd": "read_active_tab"}, a)
            resp = read_json_line(b)
        finally:
            b.close()
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["cmd"], "read_active_tab")
        self.assertEqual(fake.sent[0]["id"], 42)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["title"], "Example")
        self.assertEqual(resp["text"], "hello world")

    def test_timeout_replies_error(self):
        sent = []
        bridge = host_mod.Bridge(sent.append, request_timeout=0.2)
        a, b = socket.socketpair()
        try:
            bridge.handle_mcp_request({"id": 9, "cmd": "open_and_read_url",
                                       "url": "https://example.com"}, a)
            resp = read_json_line(b, timeout=5)
        finally:
            b.close()
        self.assertFalse(resp["ok"])
        self.assertIn("did not respond", resp["error"])

    def test_bad_request(self):
        sent = []
        bridge = host_mod.Bridge(sent.append)
        a, b = socket.socketpair()
        try:
            bridge.handle_mcp_request({"id": 1, "cmd": "bogus"}, a)
            resp = read_json_line(b)
        finally:
            b.close()
        self.assertFalse(resp["ok"])
        self.assertEqual(sent, [])  # nothing forwarded to the extension

    def test_stale_reply_ignored(self):
        sent = []
        bridge = host_mod.Bridge(sent.append)
        # reply for an unknown id: must not raise, must not send anything
        bridge.handle_extension_message({"id": 12345, "ok": True})
        self.assertEqual(sent, [])


class TestMcpServer(unittest.TestCase):
    def rpc(self, method, params=None, req_id=1):
        req = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            req["params"] = params
        return mcp_mod.handle_rpc(req)

    def test_initialize(self):
        resp = self.rpc("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "browser-buddy")

    def test_notification_gets_no_response(self):
        self.assertIsNone(self.rpc("notifications/initialized", req_id=None))

    def test_tools_list(self):
        resp = self.rpc("tools/list")
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, ["read_active_tab", "open_and_read_url"])

    def test_unknown_method(self):
        resp = self.rpc("nope/method")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_tools_call_with_stubbed_host(self):
        orig = mcp_mod.call_host
        mcp_mod.call_host = lambda cmd, args=None, timeout=75: {
            "id": 1, "ok": True, "url": "https://example.com",
            "title": "Example", "text": "page body here", "truncated": False}
        try:
            resp = self.rpc("tools/call", {"name": "open_and_read_url",
                                           "arguments": {"url": "https://example.com"}})
        finally:
            mcp_mod.call_host = orig
        content = resp["result"]["content"][0]["text"]
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("https://example.com", content)
        self.assertIn("page body here", content)

    def test_tools_call_rejects_non_http_url(self):
        resp = self.rpc("tools/call", {"name": "open_and_read_url",
                                       "arguments": {"url": "file:///etc/passwd"}})
        self.assertTrue(resp["result"]["isError"])

    def test_tools_call_unknown_tool(self):
        resp = self.rpc("tools/call", {"name": "nope", "arguments": {}})
        self.assertTrue(resp["result"]["isError"])

    def test_tools_call_host_unreachable(self):
        # Point the socket helper at a path that cannot exist.
        orig = mcp_mod.socket_path
        mcp_mod.socket_path = lambda: os.path.join(
            tempfile.gettempdir(), "browser-buddy-definitely-missing-%d" % os.getpid(),
            "nope.sock")
        try:
            resp = self.rpc("tools/call", {"name": "read_active_tab", "arguments": {}})
        finally:
            mcp_mod.socket_path = orig
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("extension", resp["result"]["content"][0]["text"])

    def test_serve_loop_end_to_end(self):
        orig = mcp_mod.call_host
        mcp_mod.call_host = lambda cmd, args=None, timeout=75: {
            "id": 1, "ok": True, "url": "https://x.test",
            "title": "T", "text": "body", "truncated": True}
        try:
            stdin = io.StringIO(
                '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
                '"params":{"name":"read_active_tab","arguments":{}}}\n'
                'not json\n')
            stdout = io.StringIO()
            mcp_mod.serve(stdin, stdout)
        finally:
            mcp_mod.call_host = orig
        lines = [json.loads(l) for l in stdout.getvalue().strip().split("\n")]
        self.assertEqual(len(lines), 3)
        self.assertIn("protocolVersion", lines[0]["result"])
        self.assertIn("(truncated)", lines[1]["result"]["content"][0]["text"])
        self.assertEqual(lines[2]["error"]["code"], -32700)


class TestSocketPath(unittest.TestCase):
    def test_xdg_runtime_dir_preferred(self):
        orig = mcp_mod.socket_path
        try:
            os.environ["XDG_RUNTIME_DIR"] = "/tmp/fake-xdg-12345"
            self.assertEqual(
                mcp_mod.socket_path(),
                "/tmp/fake-xdg-12345/browser-buddy/browser-buddy.sock")
            self.assertEqual(
                host_mod.socket_path(),
                "/tmp/fake-xdg-12345/browser-buddy/browser-buddy.sock")
        finally:
            del os.environ["XDG_RUNTIME_DIR"]

    def test_falls_back_to_tempdir(self):
        old = os.environ.pop("XDG_RUNTIME_DIR", None)
        try:
            import tempfile as tf
            self.assertEqual(
                mcp_mod.socket_path(),
                os.path.join(tf.gettempdir(), "browser-buddy",
                             "browser-buddy.sock"))
        finally:
            if old is not None:
                os.environ["XDG_RUNTIME_DIR"] = old

    def test_host_dir_is_private(self):
        tmp = tempfile.mkdtemp()
        os.environ["XDG_RUNTIME_DIR"] = tmp
        try:
            d = os.path.join(tmp, "browser-buddy")
            if os.path.isdir(d):
                import shutil
                shutil.rmtree(d)
            host_mod.socket_path()
            mode = oct(os.stat(d).st_mode & 0o777)
            self.assertEqual(mode, "0o700", mode)
        finally:
            del os.environ["XDG_RUNTIME_DIR"]


class TestDomainAllowlist(unittest.TestCase):
    def setUp(self):
        mcp_mod._config_cache = None
        mcp_mod._allowed_warned = False

    def tearDown(self):
        mcp_mod._config_cache = None
        mcp_mod._allowed_warned = False

    def _set_domains(self, domains):
        mcp_mod._config_cache = {"allowed_domains": domains}

    def test_empty_list_allows_all(self):
        self._set_domains([])
        self.assertTrue(mcp_mod.url_allowed("https://evil.example/x"))

    def test_listed_domain_allowed(self):
        self._set_domains(["example.com"])
        self.assertTrue(mcp_mod.url_allowed("https://example.com/page"))

    def test_subdomain_allowed(self):
        self._set_domains(["example.com"])
        self.assertTrue(mcp_mod.url_allowed("https://docs.example.com/"))

    def test_unlisted_domain_blocked(self):
        self._set_domains(["example.com"])
        self.assertFalse(mcp_mod.url_allowed("https://evil.com/"))
        # lookalike: notexample.com must not match example.com
        self.assertFalse(mcp_mod.url_allowed("https://notexample.com/"))

    def test_blocked_url_never_reaches_host(self):
        self._set_domains(["example.com"])
        called = []
        orig = mcp_mod.call_host
        mcp_mod.call_host = lambda *a, **k: called.append(a) or {"ok": True}
        try:
            resp = mcp_mod.handle_tools_call(
                {"name": "open_and_read_url",
                 "arguments": {"url": "https://evil.com/"}})
        finally:
            mcp_mod.call_host = orig
        self.assertTrue(resp["isError"])
        self.assertIn("allowed_domains", resp["content"][0]["text"])
        self.assertEqual(called, [])


    def test_empty_allowlist_warns_in_tool_result(self):
        self._set_domains([])
        called = []
        orig = mcp_mod.call_host
        mcp_mod.call_host = lambda *a, **k: called.append(a) or {
            "ok": True, "url": "https://anything.example/",
            "title": "t", "text": "page text"}
        try:
            resp = mcp_mod.handle_tools_call(
                {"name": "open_and_read_url",
                 "arguments": {"url": "https://anything.example/"}})
        finally:
            mcp_mod.call_host = orig
        self.assertFalse(resp["isError"])
        text = resp["content"][0]["text"]
        self.assertTrue(
            text.startswith("WARNING: allowed_domains is empty"),
            text[:120])
        self.assertIn("page text", text)

    def test_nonempty_allowlist_no_warning(self):
        self._set_domains(["example.com"])
        orig = mcp_mod.call_host
        mcp_mod.call_host = lambda *a, **k: {
            "ok": True, "url": "https://example.com/",
            "title": "t", "text": "page text"}
        try:
            resp = mcp_mod.handle_tools_call(
                {"name": "open_and_read_url",
                 "arguments": {"url": "https://example.com/"}})
        finally:
            mcp_mod.call_host = orig
        self.assertFalse(resp["isError"])
        self.assertFalse(
            resp["content"][0]["text"].startswith("WARNING"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
