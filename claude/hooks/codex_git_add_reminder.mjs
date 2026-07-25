#!/usr/bin/env node
// PreToolUse reminder: when staging files, remind to run a codex review before committing.
// Hook config: matcher "Bash", if "Bash(git add *)"
//
// Stays quiet if a Codex review already ran recently in this workspace, so
// re-staging during a single work session doesn't spam the reminder.

import { readHookInput, emitReminder, hasRecentCodexJob } from "./lib/codex-freshness.mjs";

const input = readHookInput();

// Self-validate: exit early if this isn't actually a git add command
const command = input.tool_input?.command ?? "";
if (!/^git add(\s|$)/.test(command)) {
  process.exit(0);
}

if (hasRecentCodexJob(["review", "adversarial-review"])) {
  process.exit(0);
}

emitReminder(
  "Before committing, start `codex-companion review` via the Monitor tool to get a comprehensive Codex review of the changes."
);
