---
name: claude-remote-shell
description: Run a Claude Code session whose Bash tool executes on a remote host over SSH, with the working directory kept in sync by Mutagen or by git.
disable-model-invocation: true
---

# Claude with a remote shell

`custom_bins/claude-remote-shell` redirects Claude Code's Bash tool to another machine. The agent, the model, and your terminal stay local; every command it runs goes over SSH to the host, and the working directory is kept in step by Mutagen or, on a box without it, by git.

**This skill is user-invoked only** (`disable-model-invocation: true`). Handing an agent a shell on another machine is a deliberate act, like `spawn-session` — the decision to point it at a host stays with you. If you are Claude and a task wants remote execution, print the command and let the user run it.

## Use it

```bash
cd ~/code/myproject
claude-remote-shell kit@gpu-box:/home/kit/myproject
```

Starts an ordinary Claude session in `~/code/myproject`, except that Bash runs on `gpu-box` and the two directories are synced two-way.

**It is the session you would have started by hand.** Your Claude configuration applies unchanged — settings, plugins, MCP servers, channels. The script adds a plugin for session bookkeeping and an appended system prompt describing the remote environment; nothing else about the session differs.

## Prerequisites

| Need | Why |
|---|---|
| `mutagen` on PATH | The default file sync. Without it the script falls back to git sync, and exits only if that isn't possible either |
| `jq` on PATH | The session hooks and the VCS-ignore check parse JSON with it |
| SSH to the host, host key already accepted | Connections use `StrictHostKeyChecking=yes`, so a first-contact prompt fails every Bash call. `ssh <host>` once by hand first |
| `claude` on PATH | Launched through your login shell, so aliases and functions resolve |

## The two modes

| Invocation | What happens |
|---|---|
| `claude-remote-shell host` | Bash goes remote, no project sync. The remote runs `cd $PWD` — **the same absolute path must exist on the host**, or every command fails before it starts |
| `claude-remote-shell host:/remote/path` | Two-way Mutagen sync between your cwd and `/remote/path`, plus path remapping |

Path remapping is what makes the second mode feel local: your local path is rewritten to the remote one in every command sent, and rewritten back in stdout and stderr, so paths in output match the ones you can open. Syncs are flushed before and after each command, which is how a remote build's artifacts appear locally.

Mutagen runs on a session-private daemon (`MUTAGEN_DATA_DIRECTORY` inside the session tempdir), so teardown does not touch syncs you set up elsewhere. Everything — the tempdir, the sync sessions, the SSH control socket, the remote session directory — is removed on exit.

## Two ways to move the files

Mutagen is the default. `--git` (or `--no-mutagen`, or `CLAUDE_REMOTE_SHELL_SYNC=git`) selects git instead, and git is also chosen automatically when Mutagen isn't installed and you're sitting in a repo — that case used to be a hard exit. `--mutagen` forces the default back. The startup banner says which one you got.

Git mode keeps the same feel — you `Edit` locally, Bash runs remotely — by snapshotting the worktree before each command rather than by watching the filesystem. The snapshot is built in a scratch index and pushed as a commit on `refs/claude-remote/<session>`, so **your HEAD, your index, your branches and your stash are never touched**; the remote checkout hard-resets to it, and whatever the remote changed is read back into your worktree afterwards. Both refs are deleted at teardown.

| | Mutagen | git |
|---|---|---|
| Needs | `mutagen` installed | a git repo, and cwd **must** be its root |
| Destination | `host` or `host:/path` | `host:/path` only |
| Untracked files | synced | synced |
| Files matched by `.gitignore` | synced | **never leave your machine** — build them on the remote |
| Files the remote creates | come back | come back; untracked ones also survive on the remote across commands |
| Cost per Bash call | a sync flush | a push, a remote reset and a fetch — noticeably slower |

The `.gitignore` exclusion is the one that bites: `.env`, `node_modules/`, `.venv/` and build output simply don't exist on the host under git mode. That is also what makes it safe to point at a shared box. The remote checkout carries a synthetic history belonging to the transport, so `git log` and `git status` there describe the sync rather than your project — the agent is told not to commit or branch on the remote.

## Which shell runs remotely

The script probes the remote account's login shell at startup and runs commands under `zsh` if that's what it is, `bash` otherwise. `CLAUDE_REMOTE_SHELL_SHELL=bash` or `=zsh` forces it; anything else is rejected, because `CLAUDE_CODE_SHELL` only accepts a path containing one of those two names. The chosen shell is echoed on startup and named in the agent's system prompt.

A non-interactive `zsh -c` reads `.zshenv` but not `.zshrc`, so PATH additions that live in `.zshrc` are invisible to remote commands. Move them to `.zshenv` on the host, or force `bash`.

**Tune the sync with `.claude/remote-shell/mutagen.yml`** in the directory you launch from. It is passed to Mutagen with `-c` and applies to both the project sync and the tempdir sync. This is where you exclude build outputs, or ignore VCS — note that ignoring VCS means no `.git` on the remote, and the script then tells the agent that git is unavailable.

## Variants and arguments

| Form | Effect |
|---|---|
| `claude-remote-shell host [claude args...]` | Everything after the destination is passed through to Claude |
| `claude-remote-shell --git host:/path` | Git sync instead of Mutagen. `--no-mutagen` is a synonym; `--mutagen` forces the default. Flags go **before** the destination |
| `claude-remote-shell host <claude-bin> [args...]` | A second positional that resolves to a command is used as the Claude binary instead of `claude` |
| `claude-remote-shell-yolo host` | Same, plus `--allowedTools 'Bash(*)'`. A symlink — the `*-yolo` basename is the switch |
| `claude-remote-shell host remote-control` (or `rc`) | Starts under Remote Control, with the plugin and system prompt re-injected into each spawned session |

`claude-remote-shell-yolo` gives an agent unprompted shell access to another machine. Use it where you have already accepted that risk — a throwaway box, a container — not on a host that matters.

## Inside a remote session

The script appends most of this to the agent's system prompt, so a session already knows it. It is here for when you are reasoning about behaviour from outside:

- Bash is remote and there is no local shell. Read, Write, and Edit still act on local files — with `host:/remote/path` those reach the host through the project sync; with `host` alone there is no project sync, so a local edit is invisible to Bash.
- `$TMPDIR` is a synced session directory. `/tmp` on the remote is not synced and its contents never come back.
- Subagent output comes from the TaskOutput tool. Subagents run locally, so reading their files with Bash looks on the wrong machine.
- Git may be unavailable when the Mutagen config ignores VCS.

## Source of truth

`custom_bins/claude-remote-shell` is the authority on flags, environment variables, and sync behaviour — read it when something here does not match what you see. The same file is re-entered under five names (launcher, `bash` shell wrapper, `claude-remote-control`, `session-start`, `session-end`), dispatched on `basename "$0"`.
