# Deployment Components

Reference for every component `deploy.sh` deploys, with the non-obvious rationale for each. Read this when adding, debugging, or disabling a deploy component; the full mechanics live in the matching `deploy_*()` function in [`../deploy.sh`](../deploy.sh).

Per-component subtleties (symlink vs copy, merge behaviour, key gotchas) are tabulated in [`../CLAUDE.md`](../CLAUDE.md) under *Important Behaviors*.

Each component in `deploy.sh` is deployed with inline logic or helper functions:

- ZSH configuration - Main shell setup
- Tmux configuration - Shell multiplexer config + TPM plugins (resurrect, continuum) for session persistence
- Gist sync - Bidirectional sync of SSH config and git identity with GitHub gist, automated daily at 8 AM
- Git config - Smart conflict resolution with user prompts
- VSCode/Cursor/Antigravity settings - Merges with existing settings
- Finicky - Browser routing (macOS only, symlinked)
- Ghostty - Terminal emulator configuration (symlinked to platform-specific path)
- Zed - Editor config (settings + keymap, symlinked to ~/.config/zed/)
- gitui - Theme (symlinked to ~/.config/gitui/theme.ron). Theme-reactive: uses named ANSI colors so gitui inherits whichever Ghostty theme the active window uses (default, g0-g9, SSH themes). Fixes gitui's default `disabled_fg: DarkGray`, which is unreadable on Catppuccin Mocha and similar dark backgrounds.
- Claude Code - AI assistant configuration (symlinked)
- Codex - CLI tool configuration (symlinked)
- Serena - MCP server configuration (symlinked, dashboard auto-open disabled)
- Mouseless - Keyboard-driven mouse control (macOS only, copied not symlinked)
- Alfred prefs repair - Fixes Dropbox-synced Alfred breakage (macOS only): strips `com.apple.quarantine` xattrs that block workflow scripts (`posix_spawn: error 1`), restores lost script `+x` bits, and seeds the per-machine summon hotkey from a golden snapshot. Runs `custom_bins/alfred-fix`; capture a new golden hotkey with `alfred-fix --capture`. Clipboard history is intentionally local-only and never syncs (Alfred design) — it starts fresh on each machine.
- Bear CLI symlink - `/Applications/Bear.app/Contents/MacOS/bearcli` → `/usr/local/bin/bearcli` (macOS only, so `bearcli` works in cron/scripts where shell aliases don't apply)
- Text replacements - Bidirectional sync with macOS + Alfred snippets (daily 9 AM, requires Full Disk Access for terminal app). macOS uses raw shortcuts; Alfred applies collection prefix at runtime (e.g., `fm.hi`)
- Encrypted secrets (BWS) - Stores API keys via Bitwarden Secrets Manager. Run `secrets-init bws` to configure.
- File cleanup - Downloads/Screenshots cleanup (macOS only, launchd)
- Claude Code cleanup - No-output-for-24h session cleanup (tmux preserved, launchd/cron)
- Claude plugin-cache cleanup - Daily 3 AM `claude-cache-clean --apply` (launchd/cron). Reaps superseded plugin versions and the abandoned `cache/temp_git_*` clones that plugin install/update leaves behind (~6MB/day combined). Scheduled via `scripts/cleanup/setup_cache_clean.sh`, gated by `--claude-cleanup`; disable with `setup_cache_clean.sh --uninstall`. The `custom_bins/claude-cache-clean-apply` wrapper exists because `schedule_daily` embeds its command as a single launchd `ProgramArguments` string, so a command with arguments can't be scheduled directly on macOS; it resolves `DOT_DIR` via `realpath "$0"` so it stays machine-portable
- AI tools auto-update - Daily update of Claude Code, Codex CLI, OpenCode, Antigravity CLI (6 AM, launchd/cron)
- Usage ping - Hourly minimal Haiku message (subscription/OAuth only, API key unset) to keep the Claude 5-hour usage window warm so capacity isn't wasted. `custom_bins/usage-ping`, scheduled at :00 (launchd/cron). Toggle with `--no-usage-ping`.
- Tmux resume - Hourly scan of all tmux panes; on a rate-limit prompt (Claude Code / Codex) sends configured keystrokes to resume. Detection anchors on durable rate-limit-state strings; the action (default `1 Enter ; continue`) is the fragile part — re-verify with `tmux-resume --dry-run` after CLI upgrades. Patterns in `config/tmux-resume-patterns.conf`. Scheduled at :05. Toggle with `--no-tmux-resume`.
- Hide idle apps (macOS only) - Polls every 60s (launchd `StartInterval`); hides (Cmd+H equivalent, via System Events `visible`) every running app that hasn't been frontmost for `HIDE_IDLE_MINUTES` (default 5), except apps listed in `config/clear_mac_apps.conf`'s `[hide-idle-exclude]` section. `[hide-idle-exclude]` is opt-out (hide-by-default) and orthogonal to `[no-touch]` (which only means "don't close/quit"); a small OS-chrome set (Finder, Dock, SystemUIServer, loginwindow, WindowServer) is hardcoded-excluded in `custom_bins/hide-idle-apps`, not user-configurable. Per-app idle state tracked in `~/.cache/hide-idle-apps/state`. Toggle with `--no-hide-idle-apps`.
- Developer config files - EditorConfig, curlrc, inputrc, .hushlogin (deployed with --editor flag)
- Global gitattributes - Binary file handling + line endings (deployed with --git-config flag)
- File associations - Set default editor for coding file types and default terminal for `.command`/`.tool` (macOS only, reads `config/macos_default_apps.conf`)
- Pueue + resource slices - Local job queue with cgroup-enforced CPU/memory limits (Linux only, systemd user slices, `j*` aliases)
- Package auto-update - Weekly upgrade + cleanup (Sunday 5 AM, brew/apt/dnf/pacman, launchd/cron)
- Package manager configs - Global npmrc, bunfig.toml, pnpm rc, uv.toml with 7-day min-release-age + ignore-scripts (symlinked)
- Dependency audit - Weekly scan for known-bad packages across all repos (Sunday 10 AM, launchd/cron)
