# Agents & Delegation

## Spawn Fewer Than You Want To

Current models reach for delegation more readily than the work justifies, so the default is a cap, not an invitation. Delegate for large, genuinely independent, parallelisable tracks — a wide multi-file investigation, several unrelated subsystems. Don't delegate what you can finish in a handful of tool calls yourself, and when one agent can do the job, send one rather than a fleet.

**Never spawn an agent to verify or double-check your own work.** Self-correction already happens without being asked; a verifier agent on top of it burns tokens and latency without buying accuracy. A second opinion is a different thing — that means a different model family and it has its own rule (`rules/second-opinions.md`).

## Never Run Detached Long-Jobs Inside a Subagent

When a subagent's turn ends, any process it backgrounded or detached is orphaned: nothing re-notifies the parent, the job dies or runs to no effect, and the agent reports `completed` anyway. (Real failure, 2026-06-15: `core:codex` backgrounded `codex exec` — burned hours, wrote nothing.) A subagent is not a durable host for background work. Launch long-running jobs from a harness-tracked mechanism that survives the launching context: `codex-companion` via the Monitor tool for Codex work, Bash `run_in_background` from *main* context, or tmux.

**A `completed` status means the subagent's turn ended, not that its child did anything.** When an agent claims it ran a detached job, check the artifact on disk before relaying success.

## CLI-Backed Agents Answer Instead of Delegating

`core:claude` and `core:gemini-cli` are Claude instances that will answer from their own reasoning rather than invoke the CLI, unless the prompt contains the literal command: `You MUST use the Bash tool to run: gemini -p "<prompt>"`, or `claude -p --model <model> --permission-mode bypassPermissions "<prompt>"`. A question-shaped prompt gets a text answer, not a delegation. `core:claude` draws on the API-billed pool — use sparingly.

Never delegate factual verification — `rules/verify-before-instructing.md`.

## Worktree Isolation Breaks on Absolute Paths

`isolation: "worktree"` sets the agent's cwd but does not rewrite paths inside the prompt — an absolute path in the brief sends the agent's writes to the main tree, silently defeating the isolation. Brief worktree agents with repo-relative paths only, and explicitly mark any genuinely-external absolute path read-only. Verify afterwards with `git -C <worktree> status`.

## Agent Results

Use the Agent tool's returned result; don't grep the `.output` file — long lines come back as `[Omitted long matching line]`.
