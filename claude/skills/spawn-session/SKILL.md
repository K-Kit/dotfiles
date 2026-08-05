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

**A directory inside a git repo becomes the repo root.** The `claude()` wrapper cds to the git root before launching (so `plansDirectory` resolves), which means `-d ~/code/repo/packages/api` starts the agent at `~/code/repo` with the whole repository in view, not just that package. `claude-spawn` prints a note when this applies. If the task must stay narrow, say so in the prompt — the working directory will not do it for you.

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

**Spawning from inside a spawned session is refused** (`CLAUDE_SPAWN_DEPTH`). `--allow-nested` overrides it, but if you're hitting this, check that you meant to. Note what this is: an inherited environment variable. It stops accidental fan-out — the failure mode where a seeded agent reads a task as "spawn more agents" and you find twelve tmux sessions. It is not a containment boundary, because anything running in that session can unset it.

**Every spawn is logged** to `~/.local/state/claude-spawn/spawn.log` — timestamp, session, directory, flags, and a truncated SHA-256 of the prompt. The prompt text itself is never written to disk. An unexpected session can be traced back to a known spawn.

The prompt is hashed in the log but it is *not* hidden from the machine: it sits in the command's argv while `tmux new-session` runs, so `ps` shows it to any local user during that window. Don't seed with something you would not paste into a shared terminal.

The log is append-only and nothing reaps it — one short line per spawn, so it will not matter for years, but it is not self-limiting either. Truncate it by hand if it ever does, or wire it into the same daily hook as `claude-jobs-reap`.

## Never seed with untrusted text

Do not pipe a web page, an issue body, a PR description, or a file you did not write into the seed prompt. A fresh agent has no conversation context to weigh an injected instruction against, which makes it a softer target than a session that has been running for an hour. If the task is "act on this issue", pass the issue *reference* and let the spawned agent fetch it with its own judgement intact.

## Why not hand-roll it

Three things fail silently if you script this yourself with `tmux new-session` + `send-keys`:

1. **`send-keys` races the shell.** Keys sent to a new pane land before the zsh line editor is ready and get swallowed or mangled. The script uses Claude Code's positional `[prompt]` argument instead — no timing element at all.
2. **A bare command string loses the `claude()` wrapper.** tmux runs command strings under a non-interactive shell, which sources `.zshenv` only, and `.zshenv` does not source aliases. You silently get the raw binary without venv activation, git-root cd, `CLAUDE_CODE_TASK_LIST_ID`, or channel auto-detection. The script uses `zsh -ic`.
3. **Quoting the prompt through bash → sh → zsh eats it.** The script passes it as a tmux session environment variable (`new-session -e`) and clears it from the session environment immediately after, so no quoting layer ever sees the text and it never lingers in `tmux show-environment`.

`claude --tmux` is not an alternative — it requires `--worktree`, so it cannot target an arbitrary directory.
