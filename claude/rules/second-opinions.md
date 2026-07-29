# Second Opinions

When a second opinion is worth its cost — the same approach has failed twice, a high-ambiguity design call, corroborating a high-stakes conclusion — get it from a **different model family**, not another call to the one already stuck. Two channels: a Fable subagent (Agent tool, `model: "fable"`) for judgment, architecture, and research taste; `codex-companion` (via the Monitor tool: `adversarial-review` for diffs, `plan-review` for plans, `task` for investigation) for concrete code-level critique. As of 2026-07-28 these are the two non-Opus/Sonnet families wired in; if one disappears, drop that channel — if both do, delete this rule rather than updating it.

Unlike `advisor`, neither has memory of the conversation — brief them fresh with full self-contained context.
