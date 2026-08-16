# Herdr Evaluation — Cross-Session Management & Orchestration

Evaluated 2026-08-16 against herdr.dev v0.8.0 and this repo's session tooling. Research was grounded on the vendor's `llms-full.txt` documentation bundle, the GitHub API, registry APIs, and an independent Better Stack guide; every load-bearing claim below traces to one of those (§ Sources).

## Verdict

**Recommendation: do not adopt Herdr as the session runtime today. Close the actual gap — cross-session visibility — natively, by consuming the live session registry Claude Code already maintains at `~/.claude/sessions/*.json`, which nothing in these dotfiles currently reads. Revisit Herdr on the concrete triggers in § Revisit triggers.** Confidence ~85% for a Claude-only fleet; the adopt/trial/skip call is Yulong's (§ Decision).

The reasoning in one breath: Herdr's core value for us — knowing which terminals hold agents and what state each is in — duplicates state Claude Code already publishes authoritatively, but Herdr obtains it for Claude by screen-scraping (Claude Code is a "session identity only" integration, not a lifecycle-authority one), and Herdr's runtime is an either/or replacement for the tmux-based `cw` workflow rather than an additive install.

## What Herdr is

Herdr ("the runtime your coding agents live on") is a Rust client-server terminal runtime: a background server owns real PTYs, and every UI — its Ratatui TUI, its CLI, a plain SSH session — is a detachable client. In the vendor's words: "tmux keeps terminals alive; so does Herdr. The difference is Herdr knows which terminals are agents, what state each one is in, and how to wait on them. tmux sees panes."

On top of pane persistence it adds an agent state machine (`working` / `blocked` / `done` / `idle` / `unknown`) surfaced in a sidebar and over a newline-delimited-JSON Unix-socket API (~90 methods) with genuine push events (`pane.agent_status_changed`, `pane.agent_detected`, `output_matched`, `worktree.*`). The scriptable primitives are the novel part: `herdr agent wait <name> --until blocked`, `herdr agent prompt <name> "..." --wait`, `herdr agent read <name> --source recent-unwrapped`. Git worktrees are first-class (`herdr worktree list/create/open/remove`, "worktrees are normal Herdr workspaces with Git checkout provenance"), and `herdr --remote ssh://user@host` runs the server on a remote box with a thin local client.

Facts and maturity, measured 2026-08-16: v0.8.0, Apache-2.0, Herdr Inc (YC-backed), 29,601 stars / 2,101 forks, repo created 2026-03-27, 54 stable releases in 142 days, last push the day before this evaluation. Docs are unusually complete (20 pages, machine-readable `llms*.txt` bundles, a shipped agent skill). No MCP server (zero mentions across the full 244 KB docs bundle). No published pricing; sponsorships closed, enterprise by email. **Bus factor ≈ 1**: 1,139 commits by the primary author, next human contributor has 14.

## How Herdr sees Claude Code — the load-bearing detail

Herdr has three detection layers, in descending authority: lifecycle hooks (the agent reports its own state), screen manifest (TOML rules matched against the pane's bottom buffer), and foreground-process detection. Of its 16 integrations only 6 have lifecycle authority (Pi, OMP, Kimi, OpenCode, Kilo, MastraCode). **Claude Code is in the other bucket**, quoted verbatim from the docs: "The hook reports Claude Code session identity to the local Herdr socket on session start. Claude Code state comes from Herdr's screen manifest detection."

So for a Claude-only fleet, Herdr's headline feature — semantic agent state — is inference from screen contents, with `idle` as the explicit fallback for unmatched screens (`default_known_agent_idle_fallback`). Meanwhile Claude Code itself writes `~/.claude/sessions/<pid>.json` records carrying `sessionId`, `cwd`, `pid`, `status` (busy/idle), `statusUpdatedAt`, `name`, `kind`, and a `messagingSocketPath`, plus per-session IPC sockets under `/run/user/1000/cc-socks/` and team rosters under `~/.claude/teams/`. A grep across `custom_bins/`, `config/aliases/`, `claude/hooks/`, and `tools/claude-tools/src/` finds **zero consumers** of any of it.

## What this repo already covers, and the real gap

| Concern | Today | Herdr's offer |
|---|---|---|
| Session birth | `claude()` wrapper, `cw`/`cwy` worktree+tmux pairing, `claude-spawn` (tested argv, exit-code contract) | Workspaces + `worktree create/open`, `agent start --kind claude` |
| Persistence / detach | tmux with `destroy-unattached off`, 100k-line history | Server-owned PTYs, snapshot restore, `--handoff` live upgrade |
| Per-session health | Watchdog hooks (OOM, death, working/idle marks) | Screen-scraped state machine |
| Cross-session visibility | **Nothing** — statusline is single-session; `cwclean` liveness is `tmux has-session` only | TUI sidebar + socket events; the strongest part of its offer |
| Wait/orchestrate primitives | Harness-native: `ListAgents`, `SendMessage`, background jobs, Monitor | `agent wait --until`, `agent prompt --wait` — but scraped state for Claude |
| Worktree lifecycle | `cw`/`cwmerge`/`cwrm`/`cwport`/`cwclean` | `worktree` methods (never deletes branches) |
| Remote | `hz` cloud boxes + `claude-remote-shell` + tmux over SSH | `herdr --remote ssh://` |

The gap is precisely one row: cross-session visibility. And the data to fill it natively already exists, unread.

## The tmux either/or

Herdr runs happily *inside* tmux as the outer environment, but tmux *inside* a Herdr pane blinds agent detection — "Herdr sees `tmux` as the pane process instead of the agent behind it" (vendor docs, verbatim). The `cw` workflow *spawns tmux*. That leaves two coherent shapes, not an additive install: Herdr-outer (retire `cw`'s tmux pairing, migrate to Herdr workspaces/worktrees) or tmux-outer (keep `cw`, run Herdr inside a pane where its workspace and worktree model duplicates existing tooling and cannot see the tmux-hosted sessions). Herdr's own "pairs happily with a worktree manager" claim reads as being about diff-review tools, not tmux-spawning session managers.

## Collisions and risks (if adopted or trialed)

- **`herdr integration install claude` edits `~/.claude/settings.json`** — in this setup that file is a symlink into this repo, the protected global source of truth (`.claude/rules/dotfiles-settings.md` exists precisely because third-party rewrites of it are the failure mode). Mitigation if trialing: let it install once in a scratch `CLAUDE_CONFIG_DIR`, then vendor the emitted hook entries into the dotfiles-managed file deliberately, config-first.
- **Statusline parity cost** — consuming `pane.agent_status_changed` in the statusline means implementing it twice (`statusline.rs` + `statusline.sh`) under the parity test, or accepting a break.
- **Bus factor ≈ 1** on a tool that would own every terminal the work lives in; high release cadence cuts both ways (fast fixes, moving target).
- **Distribution hygiene**: official installs are curl-pipe-sh / brew / mise / Nix; crates.io is stranded at 0.1.0 and npm is a reserved placeholder — `cargo install herdr` silently yields a five-month-old build.
- **No MCP surface** — orchestration from inside a Claude session would shell out to `herdr` CLI or speak the socket directly.

## Options

**A. Native visibility layer (recommended).** Consume the registry Claude Code already maintains. This PR includes the proof of concept: `custom_bins/claude-sessions`, a read-only stdlib-Python lister (table / `--json` / `--all`-including-stale) over `~/.claude/sessions/*.json`. Natural follow-ons, each a separate scoped decision: teach `cwl`/`cwclean` to use registry status instead of bare `tmux has-session`; a busy/idle fleet count in the statusline (parity cost applies); a watcher that surfaces long-idle or orphaned sessions. Zero new runtime, authoritative rather than scraped state, keeps `cw` untouched. What it does not give: a TUI dashboard, cross-CLI agents (codex/gemini panes), or the remote-server attach model.

**B. Bounded Herdr trial.** Herdr-outer in one terminal for a small subset of sessions for 1–2 weeks, tmux workflow untouched elsewhere (the two coexist as parallel worlds; Herdr simply cannot see the tmux ones). Vendor the Claude hook entries into `claude/settings.json` by hand as above. Success criteria worth writing down beforehand: does screen-scraped state track reality; does `agent wait --until blocked` change how work is orchestrated; is the TUI worth living in.

**C. Full adoption.** Retire the `cw` tmux pairing; Herdr workspaces + worktrees + `--remote` become the session substrate. Largest payoff if the fleet ever becomes multi-CLI, but today it means migrating a working, tested workflow onto scraped Claude state and a bus-factor-1 dependency.

## Revisit triggers

- Claude Code gains **lifecycle authority** in Herdr (hook-reported state rather than screen manifest — check `herdr integration status` on future releases).
- The fleet becomes genuinely **multi-CLI** (codex/gemini/etc. running as peers, where Herdr's uniform state model beats per-harness registries).
- The native path proves insufficient in practice — e.g. a real need for push-event orchestration or a live TUI that polling a flat-file registry can't support cleanly.

## Open question, mostly resolved while building the PoC

The biggest open question was whether the native registry can distinguish a session blocked on a permission prompt — Herdr's `blocked` state — from ordinary busy/idle. Observation alone said no: a live polling window showed only `busy` and `idle` (plus one SDK session with no `status` key at all). But the Claude Code 2.1.233 bundle validates `status` against `["busy","shell","idle","waiting"]`, so a `waiting` state exists in the schema (alongside `shell`; session `kind` further distinguishes `interactive`/`bg`/`daemon`/`daemon-worker`). Herdr's `blocked` detection therefore very likely has a cheap native equivalent; what remains unverified is watching `waiting` actually fire during a permission prompt. Two operational caveats surfaced by the PoC: the registry self-cleans on graceful exit (stale records only survive crashes/kills), and `os.kill`-based liveness is wrong inside a sandboxed Claude Bash session (different PID namespace, everything reads dead) — run the tool, and any tests of it, from a normal shell or with the sandbox disabled.

## Sources

Vendor: `herdr.dev/llms-full.txt` (4,680 lines — architecture, detection layers, integration taxonomy, socket API, worktree/plugin/config surface, Claude hook behavior, tmux caveat), `herdr.dev/compare/`, `sitemap-0.xml` (no-pricing proof), `SPONSORS.md`. GitHub API: repo, releases (complete list), top-12 contributors. Registries: crates.io API, npm registry. Independent: Better Stack guide (Stanley Ulili, 2026-06-08; corroborates architecture, TUI, remote flow; source of "single full-time developer" and "#1 GitHub Trending June 30, 2026"). Terminal Trove and Moshi mentions were discounted as former sponsors. Repo-side facts from a read-only survey of this repository and the live `~/.claude/sessions/` registry on 2026-08-16.
