# Browser Automation

| Target | Tool |
|---|---|
| User's live browser, existing tabs | claude-in-chrome (main context only — never subagents) |
| Local dev server, your own app | Playwright MCP |
| Authenticated site from a subagent | `agent-browser --profile Default` |
| Public page | WebFetch or `any2md` |

`--profile` only applies when the daemon STARTS, so a stale daemon inherits the wrong session: `agent-browser close 2>/dev/null; sleep 1; agent-browser --profile Default open <url>`.
