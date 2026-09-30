---
title: "I gave my coding agent eyes on my real, logged-in browser (stdlib-only, local)"
tags: showdev, opensource, mcp, python
canonical: (will point to README / GitHub repo)
---

Every agentic coding tool hits the same wall eventually: the page you need is behind a login, a 403, or bot detection, and the agent's server-side fetcher is locked out. Your browser is logged in. Your agent is not. So the fix is not a better scraper — it is letting the agent *borrow your eyes*.

I built [browser-buddy](https://github.com/hahahahahahahahah6/browser-buddy), an MCP server + Chrome extension (Manifest V3) that lets coding agents read pages through the user's real Chrome profile: cookies, sessions, logins included. MIT licensed, zero dependencies, nothing leaves your machine.

## The architecture (boring on purpose)

Three pieces, each dumb and replaceable:

1. **Chrome extension (MV3)** — content scripts extract the readable text of a tab on demand. It never acts on its own; it only responds to messages.
2. **Native host (Python, stdlib)** — bridges the extension to the outside world over a Unix socket in the temp dir. `chrome.runtime.connectNative` on one side, a socket server on the other.
3. **MCP server (Python, stdlib)** — speaks JSON-RPC 2.0 over stdio, exactly the framing MCP stdio servers use. No `mcp` SDK package; the protocol surface I need is two tools, so I hand-rolled the framing in ~200 lines:

```python
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
            "its readable text, then close the tab."
        ),
        ...
    },
]
```

Message flow for `open_and_read_url`:

```
agent ──JSON-RPC/stdio──▶ MCP server ──Unix socket──▶ native host
                                                          │ connectNative
                                                          ▼
                                                   Chrome extension ──▶ tab
```

Logs go to stderr (stdout is reserved for protocol messages — the classic stdio footgun, handled once and never again).

## Why not just use a headless browser?

Headless browsers are *new* browsers: no cookies, no sessions, and they get fingerprinted as bots — the exact problem I was trying to escape. The insight is embarrassingly simple: the most authenticated, least-bot-detected browser in existence is the one the user is already using. So don't automate a fake browser; ask the real one to read.

## The security model

This design is only acceptable because everything is local:

- No server, no cloud, no telemetry. The page content goes from your tab to your agent's context, period.
- The extension cannot initiate anything — it is purely reactive to tool calls the agent makes.
- The MCP server only runs when your agent starts it (stdio), and only your local machine can reach the socket.

## Verification

17/17 smoke tests pass (framing, tool schemas, socket relay, tab extraction mocks). Next: real-world testing on Windows Chrome, then the official MCP registry.

Try it: `uvx browser-buddy-mcp` (needs the extension + host from the repo).

---

*Built in the open. Issues and PRs welcome — especially from people whose agents keep getting 403'd.*
