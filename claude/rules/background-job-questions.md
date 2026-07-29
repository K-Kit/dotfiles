# Asking Questions From Background Jobs

In a background job (`~/.claude/jobs/`), a prose question reaches nobody — it renders into a job list that fires no notification; the session looks stalled. **Every decision point MUST go through `AskUserQuestion`**, then `needs input:` on its own line. Confirmations, option menus and "OK to proceed?" are decision points too.

The escape hatch is not "ask in prose" — it is **don't ask**: when the decision is scoped, low-risk and reversible, take the default, state the assumption, keep working. Nothing wider. An unscoped, irreversible or security-sensitive call stays the user's however obvious one option looks; on conflict, ask.

A subagent without `AskUserQuestion` in its `tools:` must not try — it returns the options plus a recommendation flagged `AMBIGUOUS:` for the caller to raise.

Stop-time backstop: `nudge_bg_prose_question.sh`.
