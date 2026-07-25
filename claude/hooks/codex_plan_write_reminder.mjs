#!/usr/bin/env node
// PreToolUse reminder: when writing a plan file, remind to run codex plan-review.
// Hook config: matcher "Write" (global, all repos use plansDirectory "plans" per claude/settings.json)
//
// Stays quiet if a plan review already ran recently in this workspace, so
// iterating on a plan across several Write calls doesn't spam the reminder.

import { readHookInput, emitReminder, hasRecentCodexJob } from "./lib/codex-freshness.mjs";

const input = readHookInput();
const filePath = input.tool_input?.file_path ?? "";

// Only remind for plan files (project-root plans/*.md, per this global plansDirectory setting)
if (!filePath.includes("/plans/") || !filePath.endsWith(".md")) {
  process.exit(0);
}

if (hasRecentCodexJob(["plan-review"])) {
  process.exit(0);
}

emitReminder(
  "After finalizing this plan, start `codex-companion plan-review " +
    filePath +
    "` via the Monitor tool to get Codex feedback. Address any comments, or escalate to the user if unsure."
);
