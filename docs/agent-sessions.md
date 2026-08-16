# agent-sessions — Native Fleet Visibility & Orchestration

`custom_bins/agent-sessions` is the native implementation of the session-management features the [Herdr evaluation](herdr-evaluation.md) recommended building instead of adopting Herdr: fleet visibility, wait-until-state orchestration, state-change notifications, and jump-to-agent — over state that Claude Code and codex-companion already publish, with no daemon and no screen-scraping.

## Sources

| Source | Where | State quality |
|---|---|---|
| `claude` | `$CLAUDE_CONFIG_DIR/sessions/*.json` (default `~/.claude/sessions/`) | Authoritative: Claude Code maintains `status` (`busy`/`shell`/`idle`/`waiting`), name, cwd, pid, kind |
| `codex-job` | `$CLAUDE_CONFIG_DIR/plugins/data/codex-*/state/*/jobs/*.json` | Authoritative: codex-companion job status, pid, workspace, timestamps |
| `codex-proc` | `/proc` scan for `codex` processes (Linux only) | Presence only: pid, cwd, start time — status is always `running` |

Liveness everywhere is `os.kill(pid, 0)` plus the registry's `procStart` start-time token (guards recycled pids). A `running` codex job whose pid is dead renders `(stale)`; the Claude registry self-cleans on graceful exit, so its stale records are crash debris.

## Commands

```
agent-sessions                          # table: NAME STATUS AGE PID SOURCE KIND CWD, attention-first
agent-sessions --all --json             # everything incl. stale/terminal, machine-readable
agent-sessions wait wt-x --until idle   # block until a session/job reaches a state
agent-sessions wait review-abc --until done --timeout 600
agent-sessions watch --notify           # one line per state transition; desktop-notify on attention
agent-sessions attach wt-x              # jump to the tmux pane hosting that session
agent-sessions in-use <path>            # exit 0 if a live agent works under path (cwclean/cwrm guard)
agent-sessions worktrees                # git worktree list joined with live agents per tree
```

Targets for `wait`/`attach` are a session name, pid, job id, or a unique name/sessionId prefix. `--source claude,codex-job,codex-proc` filters any command.

Exit codes: `0` ok/reached, `1` not found/not in use, `2` ambiguous target, `3` wait timeout, `4` wait target missing or ended before reaching the state. `wait --until done` means "no longer working" — a codex job went terminal or a Claude session exited; `gone` means the record vanished or its pid died.

## Shell integration

`cwl` now renders `agent-sessions worktrees` (worktree list + live agents per tree), falling back to plain `git worktree list` when the tool is unavailable. `cwclean` keeps a worktree whose registry/codex state shows a live agent cwd'd inside it (`in-use` status) even when its tmux session is gone — previously `tmux has-session` was the only liveness signal, which missed sessions launched outside the `cw` pairing. `cwrm` refuses (without `--force`) to remove a worktree hosting a live agent.

## Orchestration recipes

```sh
agent-sessions wait my-feature --until waiting && notify-send "needs approval"
agent-sessions wait review-job --until done --timeout 900 && codex-companion result
agent-sessions watch --json | while read -r ev; do ...; done
```

## Caveats

- **Sandboxed Claude Bash sessions see a different PID namespace** — every liveness check reads dead and `/proc` scanning finds nothing. Run `agent-sessions` (and its tests) from a normal shell, or with the sandbox disabled.
- `codex-proc` and `procStart` verification are Linux-only; on macOS the claude and codex-job sources still work, liveness falls back to bare `os.kill`, and `pid_ancestors` falls back to `ps`.
- The `waiting` status exists in Claude Code's registry schema (validated against `["busy","shell","idle","waiting"]`) but has not yet been observed firing during a live permission prompt — `wait --until waiting` is wired and will work as soon as the harness writes it.
- Statusline fleet counts were deliberately left out: the statusline has two implementations that must stay in parity (`statusline.rs` + `statusline.sh`) and the `darwin-arm64` binary cannot be rebuilt from Linux, so that follow-on needs its own PR.

Tests: `tests/test_agent_sessions.py` (subprocess tests against a synthetic `CLAUDE_CONFIG_DIR`, unit tests via module import).
