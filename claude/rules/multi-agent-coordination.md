# Multi-Agent Coordination

Multiple agents (Claude Code, Codex, Cursor, Gemini) may share one working tree. **Agents in different worktrees need no coordination at all** — isolated files, merged later. So an overlapping claim means: move to a worktree (`cw <name>`), or wait for the other agent.

Claims ("chope") live in `.agent-claims/` at the repo root, one file per agent named by `$PPID` — not `$$`, which is an ephemeral subshell. Claims are **advisory**: they surface conflicts, they don't lock.

**Before starting:** read every file in `.agent-claims/`, reaping dead ones first — `kill -0 <pid>` failing means stale; for remote agents (no live PID on this machine) treat a `since:` older than 2h as stale. **When done or switching tasks:** `rm -f .agent-claims/$PPID` — an ephemeral coordination artifact, exempt from the no-`rm` rule.

Claim file — plain `key: value` lines:

```
agent: claude-code | codex | cursor | gemini | human
pid: $PPID
branch: <branch>
worktree: <abs path>
files: <explicit paths, not globs — overlap is detected at file level>
task: <what you're doing; note sub-file granularity here>
since: <ISO-8601 UTC>
```

This practice is live because the harness auto-approves its file operations — `rg -q 'agent-claims' claude/hooks/auto_classify.py` (true as of 2026-07-27). If that regex is ever removed, every claim operation starts prompting, nobody will keep doing this, and the rule should be deleted rather than kept as aspiration.
