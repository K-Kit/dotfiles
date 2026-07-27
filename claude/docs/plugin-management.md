# Plugin Organization & Context Profiles

**Always-on plugins** are whatever `base:` lists in `profiles.yaml`; they load in every session regardless of profile. Everything else is profile-gated, so the enabled set differs per project — never assume a count. To see what is actually on right now: `claude-tools context --list`.

**ai-safety-plugins** (`github.com/yulonglin/ai-safety-plugins`):
- `core` — foundational agents, skills, safety hooks (always-on)
- `research` — experiments, evals, analysis, literature
- `writing` — papers, drafts, presentations, multi-critic review
- `code` — dev workflow, debugging, delegation, code review
- `workflow` — agent teams, handover, conversation management, analytics
- `viz` — TikZ diagrams, Anthropic-style visualization

**Context profiles** control which plugins load per-project via `claude-tools context`:
```bash
claude-tools context                    # Show current state / apply context.yaml
claude-tools context code               # Software projects
claude-tools context code typescript python    # Compose multiple profiles
claude-tools context --list             # Show active plugins and available profiles
claude-tools context --clean            # Remove project plugin config
claude-tools context --sync [-v]        # Register + update + install wanted + prune orphans
claude-tools context --sync --no-prune  # Sync without removing orphan plugins
```

**Unified repo setup** via `claude-tools setup`:
```bash
claude-tools setup                      # Auto-detect + run needed setup steps
claude-tools setup secrets              # Interactive secret picker (delegates to setup-envrc)
claude-tools setup context              # Plugin profile picker (delegates to context)
```

**Architecture:**
- Plugin registry: auto-discovered from `~/.claude/plugins/installed_plugins.json` (source of truth)
- Marketplace manifest: `~/.claude/templates/contexts/profiles.yaml` `marketplaces:` section (declarative)
- Profile definitions: same file, `base:` + `profiles:` sections (per-profile enables)
- Per-project config: `.claude/context.yaml` (profiles + optional enable/disable overrides, committed)
- Output: `.claude/settings.json` `enabledPlugins` section (deterministic rebuild from profiles)
- CLI args persist to `context.yaml` automatically; no-arg invocation re-applies it
- SessionStart hook auto-applies `context.yaml` on every session start
- Statusline shows active context profiles (e.g., `[code python]`)

Adding a new plugin: add its marketplace to `marketplaces:` in `profiles.yaml`, run `claude-tools context --sync`, then add to a profile.

## Renaming a local marketplace plugin

Update these four locations, then restart Claude Code:

1. **Source**: rename dir `claude/ai-safety-plugins/plugins/<old>/` → `<new>/`, update `"name"` in `.claude-plugin/plugin.json`
2. **Marketplace manifest**: update the entry in `claude/ai-safety-plugins/.claude-plugin/marketplace.json`
3. **settings.json**: change `"<old>@ai-safety-plugins"` → `"<new>@ai-safety-plugins"` in `enabledPlugins`
4. **Clear cache**: remove `~/.claude/plugins/cache/ai-safety-plugins/<old>` (re-created on next `/plugin` install)

## Stopping Serena's dashboard from auto-opening

Serena's web dashboard opens in the browser on every new session unless the MCP server is started with `--open-web-dashboard false`. Add the flag to the server args in `claude/plugins/marketplaces/claude-plugins-official/external_plugins/serena/.mcp.json`:

```json
{
  "serena": {
    "command": "uvx",
    "args": [
      "--from", "git+https://github.com/oraios/serena",
      "serena", "start-mcp-server",
      "--open-web-dashboard", "false"
    ]
  }
}
```

That file lives in the plugin marketplace cache (`plugins/marketplaces/`), which is gitignored — so the change must be reapplied after clearing the plugin cache or on a new machine, and needs a full Claude Code restart to take effect. The dashboard stays reachable at `http://127.0.0.1:24286/dashboard/index.html` when you actually want it; `~/.claude/logs/mcp-serena.log` is the place to check if it misbehaves.
