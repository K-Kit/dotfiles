---
name: spawn-session
description: Spawn a detached tmux session running Claude Code in a chosen directory, seeded with an initial prompt, optionally reachable by Remote Control.
disable-model-invocation: true
---

# Spawn a seeded Claude session

`custom_bins/claude-spawn` does the whole thing in one command: new tmux session, cd to the directory, launch Claude Code, submit the seed prompt.

**This skill is user-invoked only** (`disable-model-invocation: true`). A spawned session outlives the conversation that created it and was never watched starting, so the decision to create one stays with the user. If you are Claude and a task looks like it wants a spawned agent, say so and print the command — do not reach for this yourself, and note that a subagent is usually the right tool instead.

## Use it

```bash
claude-spawn -d ~/code/some-repo "Investigate the flaky test in tests/test_auth.py and propose a fix"
```

Creates a detached session named `some-repo-MMDD-HHMM` running Claude Code in that directory with the prompt already submitted. Local-only: Remote Control is off unless asked for.

`claude-spawn --help` for the full list. The ones that matter:

| Flag | When |
|---|---|
| `-r` / `-n <name>` | You want to drive it by Remote Control from the phone. Off by default |
| `-s <name>` | The session name matters (you'll `tmux attach` to it by hand) |
| `-a` | Attach immediately instead of leaving it detached |
| `--auto` | Long unattended run that should resume itself after a rate limit — prefixes the window with the `tmux-resume` opt-in prefix |
| `--prompt-file <p>` / `-` | The seed prompt is long or multi-line |
| `-y` | Skip permission prompts. Meant for a worktree you've already accepted risk in |
| `--dry-run` | Show what would happen, change nothing |

## The gates, and why

**Remote Control is opt-in.** It widens a session from local-tty-only to account-reachable code execution. That is a real capability increase and it is never inferred — pass `-r` or `-n <name>`.

**`-y` together with `-r` is refused** unless you add `--allow-remote-yolo`. An unrestricted agent that is also drivable from off-machine is the one combination worth stopping to think about; either alone is ordinary.

**Spawning from inside a spawned session is refused** (`CLAUDE_SPAWN_DEPTH`). This caps fan-out at one generation. `--allow-nested` overrides it, but if you're hitting this, check that you meant to.

**Every spawn is logged** to `~/.local/state/claude-spawn/spawn.log` — timestamp, session, directory, flags, and a truncated SHA-256 of the prompt. The prompt text itself is never written to disk. An unexpected session can be traced back to a known spawn.

## Never seed with untrusted text

Do not pipe a web page, an issue body, a PR description, or a file you did not write into the seed prompt. A fresh agent has no conversation context to weigh an injected instruction against, which makes it a softer target than a session that has been running for an hour. If the task is "act on this issue", pass the issue *reference* and let the spawned agent fetch it with its own judgement intact.

## Why not hand-roll it

Three things fail silently if you script this yourself with `tmux new-session` + `send-keys`:

1. **`send-keys` races the shell.** Keys sent to a new pane land before the zsh line editor is ready and get swallowed or mangled. The script uses Claude Code's positional `[prompt]` argument instead — no timing element at all.
2. **A bare command string loses the `claude()` wrapper.** tmux runs command strings under a non-interactive shell, which sources `.zshenv` only, and `.zshenv` does not source aliases. You silently get the raw binary without venv activation, git-root cd, `CLAUDE_CODE_TASK_LIST_ID`, or channel auto-detection. The script uses `zsh -ic`.
3. **Quoting the prompt through bash → sh → zsh eats it.** The script passes it as a tmux session environment variable (`new-session -e`) and clears it from the session environment immediately after, so no quoting layer ever sees the text and it never lingers in `tmux show-environment`.

`claude --tmux` is not an alternative — it requires `--worktree`, so it cannot target an arbitrary directory.
