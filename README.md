# Browser Buddy

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Chrome MV3](https://img.shields.io/badge/Chrome-Manifest%20V3-blue.svg)](extension/manifest.json)
[![No dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](pyproject.toml)

**Let your coding agent read pages through YOUR real, logged-in browser.**

Claude Code's WebFetch gets 403'd on most sites. Ask it to pull up a doc, check a Reddit thread, or look at a GitHub issue, and it comes back with a 403 — because server-side fetchers don't have your cookies, your logins, or your sessions.

Browser Buddy is a small local MCP bridge: a Chrome extension (Manifest V3) talks to a Python native-messaging host, which exposes two MCP tools. Your agent reads pages exactly as you see them — logged in, past bot checks — and the page never leaves your machine except into the agent's context.

Stdlib only. No dependencies, no cloud, no API keys.

## Tools

| Tool | What it does |
|---|---|
| `read_active_tab` | Extract readable text (URL, title, cleaned body) from your active Chrome tab |
| `open_and_read_url` | Open a URL in a **background** tab with your real profile (cookies/logins), extract its text, close the tab |

## Architecture

```
┌─────────────┐   MCP (JSON-RPC 2.0     ┌──────────────────┐  Unix socket   ┌───────────────────┐  Native Messaging  ┌────────────────┐
│ Claude Code │ ── over stdio, NDJSON ──▶ │ browser_buddy_mcp  │ ── NDJSON ──▶ │ browser_buddy_host │ ── 4-byte LE ──▶ │ Chrome extension │
│             │ ◀────────────────────── │      .py           │ ◀──────────── │       .py          │ ◀── JSON ─────── │ (your profile) │
└─────────────┘                          └──────────────────┘                └───────────────────┘                  └────────────────┘
         ▲                                        │                                                                     │
         │                                        │  one request per socket connection;                                 │  chrome.tabs +
         └──────── page text lands here ──────────┘  host routes replies by request id                                  │  chrome.scripting
                                                                                                                        ▼
                                                                                                              real logged-in page
```

- **Extension** (`extension/`): MV3 service worker. Holds a persistent `chrome.runtime.connectNative` port to the host (auto-reconnects). Executes `read_active_tab` / `open_and_read_url` against your real tabs and posts extracted text back.
- **Host** (`host/browser_buddy_host.py`): launched by Chrome. Bridges the extension (length-prefixed stdio) and the MCP server (Unix socket at `$TMPDIR/browser-buddy/browser-buddy.sock`). Correlates requests by id, with a 90 s timeout.
- **MCP server** (`pypi/browser_buddy_mcp.py`): hand-rolled JSON-RPC 2.0 over stdio (no `mcp` package). Forwards tool calls to the host; returns page text as MCP `content`.

## Installation

### 1. Load the extension

1. Open `chrome://extensions`, enable **Developer mode**.
2. **Load unpacked** → select the `extension/` folder.
3. Copy the extension ID shown under "Browser Buddy".

### 2. Register the native host

```bash
cd host
./install.sh <paste-extension-id-here>
```

This writes `com.browserbuddy.host.json` into Chrome's `NativeMessagingHosts`
directory (Linux/macOS) pointing at `browser_buddy_host.py`. Restart Chrome,
then open the extension's service worker console — you should see
`[browser-buddy] native host connected`.

### 3. Add the MCP server to Claude Code

```bash
claude mcp add browser-buddy -- python3 /absolute/path/to/pypi/browser_buddy_mcp.py
```

Or in `~/.claude.json` / project `.mcp.json`:

```json
{
  "mcpServers": {
    "browser-buddy": {
      "command": "python3",
      "args": ["/absolute/path/to/browser-buddy/pypi/browser_buddy_mcp.py"]
    }
  }
}
```

Requires Python 3.9+. No pip install needed — or install the published
package: `pip install browser-buddy-mcp` (built from `pypi/`; the `browser-buddy-mcp`
command is the entry point).

## Demo

1. Log in to a site WebFetch can't reach in your normal Chrome (e.g. a private GitHub issue, a login-walled doc).
2. In Claude Code: *"Use browser-buddy to read my active tab."*
3. Or: *"Use browser-buddy's open_and_read_url to read <url>."* — a background tab opens, the text is extracted, the tab closes itself.

## Why not just use a scraping API? (Firecrawl et al.)

Vendor scrapers (Firecrawl, etc.) fetch pages **server-side**: they never have
your cookies, can't get past SSO/2FA/login walls, can't see what *you* see
behind an account, and they charge per page. The official Claude in Chrome goes
the other way — full autonomous browser control — but ships a permission dialog
per tool call (dozens per session) and sits at 2.7 stars on the Chrome Web
Store.

Browser Buddy picks the middle: **read-only access through the browser you
already logged into**. No credentials leave your machine, nothing to pay per
page, no dialog spam — just "let the agent see what I see."

## Limitations (honest)

- **Chrome/Chromium only.** The extension is the whole trick; no Chrome, no tool.
- **Read-only by design.** It extracts text; it doesn't click, type, or fill forms. That's a feature for trust, but it means CAPTCHAs / interactive gates still stop it.
- **Text extraction is heuristic**, not a full readability engine. Heavy SPAs usually work (we wait for load + a settle beat), but exotic layouts may extract noise.
- **Your real profile = real risk surface.** The agent sees everything your browser can see. Only install this if you trust the agent session reading your tabs.
- **MV3 service workers are ephemeral.** The extension auto-reconnects its native port, but a cold start can add ~1 s to the first call.
- **One request at a time per socket connection** is fine for an agent; this is not built for concurrent scraping fleets.
- Long pages are truncated at 60k characters (flagged with `(truncated)`).

## Security: domain allowlist

`open_and_read_url` opens URLs in your **real, logged-in** browser. A
prompt-injected page could otherwise steer the agent toward your email or
other sensitive sites. Restrict which domains the agent may open:

```jsonc
// ~/.config/browser-buddy/config.json
{
  "allowed_domains": ["github.com", "stackoverflow.com"]
}
```

Entries match the domain and its subdomains (`docs.github.com` is covered by
`github.com`). When the list is empty (default), any URL is allowed but the
server logs a warning recommending you set it. URLs outside the list are
refused before anything is opened. `read_active_tab` is unaffected — it only
reads the tab *you* already opened.

## Development

```bash
python3 tests/test_smoke.py   # 25 tests: framing, bridge routing, MCP handlers,
                              # socket dir, domain allowlist
node --check extension/background.js
```

## License

MIT — see [LICENSE](LICENSE).
