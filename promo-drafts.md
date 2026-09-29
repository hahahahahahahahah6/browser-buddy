# Promo drafts for browser-buddy

Repo: https://github.com/hahahahahahahahah6/browser-buddy (public, MIT)

---

## r/SideProject draft

**Title:** I built a Chrome companion for coding agents — let Claude Code read pages behind login walls

**Body:**

Every day my coding agent hits the same wall: WebFetch gets 403'd on most sites. I ask it to pull up a doc, check a Reddit thread, look at a GitHub issue — and it comes back with a 403, because it doesn't have my cookies.

So I built Browser Buddy: a Chrome extension + native messaging host + MCP server that lets your agent read pages through your real, logged-in Chrome. Two tools:

- `read_active_tab` — extract readable text from whatever tab you're looking at
- `open_and_read_url` — open a URL in a background tab (with your login state), extract the text, close it

Why not Firecrawl / API scrapers? They're server-side: no cookies, no SSO, no 2FA, and pay-per-page. Why not Claude in Chrome? Full browser autopilot with a permission popup on every tool call (their own issue #91495 admits dozens of dialogs per session) and a 2.7-star store rating. This takes the middle: read-only, no popups, no per-page fees — let the agent see what I see.

Zero dependencies (stdlib Python + MV3 JS), MIT licensed. Install is 3 steps: load the unpacked extension in `chrome://extensions`, run `host/install.sh` with your extension ID, add the MCP server to Claude Code (`claude mcp add browser-buddy -- python3 <path>/mcp_server/browser_buddy_mcp.py`).

Repo: https://github.com/hahahahahahahahah6/browser-buddy

Honest caveat: the end-to-end chain was built on a headless VM, so the extension <-> host <-> MCP path hasn't run on a real machine yet — if you try it, tell me what breaks. Especially curious: what's your current workaround for login-walled pages?
