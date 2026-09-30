# browser-buddy-mcp

The MCP server half of [browser-buddy](https://github.com/hahahahahahahahah6/browser-buddy):
lets coding agents (Claude Code, Cursor, …) read pages through your real, logged-in
Chrome browser — past login walls, 403s and bot checks that defeat server-side fetchers.

Stdlib only. No dependencies. The page never leaves your machine except into the agent's context.

Requires the [browser-buddy Chrome extension + native host](https://github.com/hahahahahahahahah6/browser-buddy)
to be installed — this package is just the MCP server process.

```
uvx browser-buddy-mcp
```

Tools: `read_active_tab`, `open_and_read_url`.
