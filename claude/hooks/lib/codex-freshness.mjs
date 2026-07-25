// Shared freshness-check for Codex-review PreToolUse reminders.
//
// Suppresses a reminder if a matching Codex job already ran recently in this
// workspace, so repeated triggers (re-staging, re-editing the same plan,
// re-running /code-review) don't nag every time. Fails open — if
// codex-companion can't be reached, treat it as "no recent job" so the
// reminder still fires rather than silently going missing.
//
// This mirrors claude/scripts/lib/hook-helpers.mjs's fetchSessionJobs(), but
// queries --workspace (all Claude sessions) instead of the current session
// only: a reminder should stay quiet if ANY recent session already reviewed,
// not just this one.

import fs from "node:fs";
import { execSync } from "node:child_process";

export const DEFAULT_FRESHNESS_MS = 30 * 60 * 1000; // 30 min

export function hasRecentCodexJob(kinds, freshnessMs = DEFAULT_FRESHNESS_MS) {
  let status;
  try {
    const raw = execSync("codex-companion status --all --workspace --json", {
      encoding: "utf8",
      timeout: 3000 // must stay under the hooks' 5s harness-level timeout so the fail-open catch below can still run
    });
    status = JSON.parse(raw);
  } catch {
    return false;
  }

  const jobs = [
    ...(status.running ?? []),
    ...(status.recent ?? []),
    ...(status.latestFinished ? [status.latestFinished] : [])
  ];

  const cutoff = Date.now() - freshnessMs;
  return jobs.some((job) => {
    if (!kinds.includes(job.kind)) return false;
    const createdAt = Date.parse(job.createdAt ?? "");
    return Number.isFinite(createdAt) && createdAt >= cutoff;
  });
}

export function readHookInput() {
  return JSON.parse(fs.readFileSync(0, "utf8"));
}

export function emitReminder(message) {
  const output = {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      additionalContext: message
    }
  };
  process.stdout.write(JSON.stringify(output) + "\n");
}
