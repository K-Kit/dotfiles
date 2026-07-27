# Desktop Control

Yulong is often physically at the machine. Ask before anything that launches a GUI app, moves focus or the cursor, types keystrokes, resizes or rearranges windows, or closes an app or tab — keystrokes land wherever focus is and can corrupt what he's typing. Read-only screenshots of already-visible apps and listing calls (`tabs_context_mcp`, `list_granted_applications`) need no permission. Authorization is per-task and doesn't carry to the next one.

The sandbox doesn't know whether Yulong is present: `dangerouslyDisableSandbox` grants filesystem access, never permission to seize the cursor.

Prefer the CLI/MCP path when one exists — Bear MCP over launching Bear, `gws` over opening a Doc in Chrome, Playwright over the user's Chrome — it removes the question entirely.
