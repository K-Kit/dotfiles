#!/usr/bin/env node
// PreToolUse reminder: when invoking the code-review skill, remind to also run
// a codex adversarial-review in the background alongside the other review agents.
// Hook config: matcher "Skill" (no `if` — Skill(code-review) pattern doesn't work)
//
// Stays quiet if an adversarial review already ran recently in this workspace,
// so re-running /code-review in the same session doesn't spam the reminder.

import { readHookInput, emitReminder, hasRecentCodexJob } from "./lib/codex-freshness.mjs";

const input = readHookInput();

// Self-validate: only fire for the code-review skill
const skill = input.tool_input?.skill ?? "";
if (skill !== "code-review") {
  process.exit(0);
}

if (hasRecentCodexJob(["adversarial-review"])) {
  process.exit(0);
}

emitReminder(
  "Before launching the code-review agents, first start " +
    "`codex-companion adversarial-review` via the Monitor tool " +
    "to run a Codex adversarial review in parallel with the other agents."
);
