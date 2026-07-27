---
name: wrap-up
description: Use when an hourly nudge asks a stalled session to conclude - drives a session to a terminating end state (land the work, state the blocker, or take one step) instead of continuing indefinitely
---

# Wrap Up

## Overview

You have been woken by the hourly `tmux-resume` nudge, not by a human. Something in this session hit a rate limit and the scheduler is prompting you to resume.

**Your job is to reach an end state, not to keep working.** Every branch below terminates. If you finish this skill still working, you have used it wrong.

**Announce at start:** "Using the wrap-up skill to bring this session to a close."

## Why this exists

The hourly nudge in `config/tmux-resume-patterns.conf` sends a bare `continue`. `continue` has no exit branch, so sessions resume hourly forever and accumulate: 101 of 157 jobs on this machine were hourly `continue` nudges, and 56 sessions sat blocked on a human decision that was never surfaced. Fixing the inflow means the nudge must be able to *end* a session.

**Status: the config still sends `continue`.** Repointing it is blocked on capturing the exact wording of a real rate-limit prompt, so this skill is currently reachable only by typing `/wrap-up` yourself. That is deliberate — the alternative was guessing a match pattern, and a wrong guess ships a nudge that silently never fires.

## Step 0: Scope guard (required first)

This skill is being trialled on `dotfiles` only. Before anything else:

```bash
git rev-parse --show-toplevel 2>/dev/null
```

**If the repo root's basename is not `dotfiles`** (including any worktree under `dotfiles/.claude/worktrees/`), stop immediately. Print one line — `wrap-up: out of trial scope (<repo>), no action taken` — and end your turn without touching the working tree. Do not fall back to `continue`. Do not do the work "just this once."

This guard exists because the nudge fires against every pane the scheduler can see, and the pattern file has no per-repo field. The skill is the only place scoping can live right now.

## Step 1: Classify honestly

Pick exactly one. The classification decides everything downstream, so get it right before acting.

| State | Test | Go to |
|---|---|---|
| **Done** | The work the session was asked to do is complete, or complete enough to land behind review | Step 2 |
| **Blocked** | Progress requires a decision, credential, or approval only the owner can give | Step 3 |
| **Mid-flight** | There is a concrete next step you can take alone, right now, without a decision | Step 4 |

**The honest classification is usually "blocked."** If you find yourself reasoning toward "mid-flight" because it feels more productive, re-read the blocked test. A session that invents a next step to avoid stating a blocker is the exact failure this skill was written to stop.

## Step 2: Done — land it and exit

**Precondition, before anything is staged: check the branch.** `git rev-parse --abbrev-ref HEAD`. If it is `main` or `master`, you may not commit. "Never push to `main`" suppresses only the *push* — committing first still lands a direct local commit on `main` with no review path, and the nudge runs unattended in whatever checkout the pane happened to be in. Either move the work to a branch (`git switch -c wrap-up/<topic>`) if it is unambiguously this session's, or classify as **blocked** (Step 3) and say the tree holds work you could not safely land. There is no third option.

1. **List the paths this session touched**, from your own transcript — not from `git status`. A shared checkout can hold edits from the user, a runtime process, or another job running concurrently, and none of those are yours to commit.
2. **Stage by name**: `git add <path> <path> …`, including new files this session created. Never `git add -u` (it sweeps up every tracked edit in the tree, including someone else's) and never `git add -A` (it also stages the sandbox's char-device masks). A blanket untracked ban is equally wrong in the other direction — it silently drops the session's own new files and the run ends having landed nothing.
3. **Verify before committing**: `git diff --cached --name-only` must match your list exactly, nothing extra. Anything modified that you cannot attribute to this session stays unstaged; name it in the PR body so the owner knows it is there. If you cannot attribute the changes at all, stop and go to Step 3 — an unattributable diff is a decision for the owner, not a commit.
4. Commit with a real message that says *why*, not just what. No heredocs — write to `/tmp/claude/<job>-msg.txt` and `git commit -F`.
5. If the branch is ahead of `main` and the tree is clean, push and open a **draft** PR (`gh pr create --draft`). Never push to `main`, never force-push, never merge.
6. Rename the session (Step 5), then **end your turn.**

If tests exist and you have not run them, run them and report the result plainly in the PR body. A failing suite does not block landing a draft PR — it blocks claiming the work is finished. Say which it is.

## Step 3: Blocked — state the decision and exit

This is the branch that matters most. Produce, in this order:

1. **The decision**, in one sentence, phrased as a question.
2. **The options**, each with its real tradeoff — not a strawman and a favourite.
3. **Your recommendation**, with one sentence of reasoning.
4. **What it gates** — what stays stuck until this is answered.

Then surface it via **`AskUserQuestion`**, not prose. This session is a background job; prose questions do not notify the owner, and an unnotified question is indistinguishable from no question at all. See `rules/background-job-questions.md`.

Then rename the session (Step 5) and **end your turn.**

> **Never choose on the owner's behalf in order to look finished.** A stalled session is visibly stalled and costs an hour. A silently-wrong autonomous choice is invisible and lands in `main`. This constraint is not negotiable for the sake of a tidier job list — if you are unsure whether a call is yours, it is not yours.

## Step 4: Mid-flight — one step, then land or state

Take **one** concrete step. Not a work session — one step, the one you already knew you needed.

Then re-classify against Step 1 and follow Step 2 or Step 3. You may not return to Step 4 twice in a row: if the next nudge finds you mid-flight again, treat that as evidence you are actually blocked and go to Step 3.

## Step 5: Rename so the list is readable

The jobs list shows `nameSource: "auto"` names that say nothing about outcome, which is why 157 entries are unreadable at a glance. Set the tmux window name to encode disposition:

```bash
tmux rename-window "done-<topic>"      # landed, PR open or committed
tmux rename-window "blocked-<topic>"   # question surfaced, awaiting the owner
```

Keep `<topic>` to two or three words. If `tmux rename-window` fails (no tmux, detached), skip it — it is a legibility nicety, not a correctness step, and it must never abort a wrap-up.

**Known limitation, stated rather than papered over:** this renames the *tmux window*, which is what you see in a terminal. The `name` field in `~/.claude/jobs/<id>/state.json` — what the agents list reads — is not writable from inside a session, and the sandbox denies writes under `~/.claude/jobs` regardless. So this improves tmux legibility only. Closing the gap for the agents list needs a change outside this skill.

## Anti-patterns

| Thought | Reality |
|---|---|
| "I'll just keep going, I'm close" | That is what `continue` did 101 times. Classify and terminate. |
| "I'll pick the sensible option and note it" | Step 3 exists precisely to stop this. Surface it. |
| "This repo isn't dotfiles but the work is obviously fine" | Step 0 is a hard stop. The trial scope is the point. |
| "I'll commit everything to be safe" | Stage the paths you touched, by name. `git add -u` commits other people's work; `git add -A` also stages sandbox char-device masks. |
| "I'm on `main` but the change is small" | Step 2's precondition is a hard stop. Unattended commits onto `main` are exactly what the draft-PR flow exists to prevent. |
| "No blocker, so I'll invent a next step" | Re-read Step 1. Inventing work to avoid stating a blocker is the failure mode. |
| "I'll ask in prose, the owner will see it" | Background jobs do not notify on prose. `AskUserQuestion` or it did not happen. |
