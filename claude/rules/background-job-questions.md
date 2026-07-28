# Asking Questions From Background Jobs

## The Rule

**In a background job, every decision point MUST go through `AskUserQuestion`. A question written as prose does not reach the user.**

Background jobs (`claude --bg` / `--background`, detached daemon sessions, anything in `~/.claude/jobs/`) render their text into a job list the user reads later, if at all. `AskUserQuestion` fires a notification; prose does not. A prose question in a background job is therefore not a question — it is a session that has silently stopped, indistinguishable from a stall, and it is the mechanism by which jobs pile up in `blocked` with nobody aware a decision was owed.

## What This Means Concretely

| Situation | Wrong | Right |
|---|---|---|
| One genuine decision to make | "Which would you prefer — A or B?" then stop | `AskUserQuestion` with A and B as options |
| Several decisions, interviewing (`/grill-me`) | Numbered list of questions in prose | One `AskUserQuestion` per decision, sequentially, waiting for each |
| A **scoped, low-risk, reversible** default exists | Ask anyway | Take the default, state the assumption in your narration, keep working |
| An **unscoped** choice — the design, framing, or approach itself — where one option merely *looks* like the default | Decide autonomously because a default is available | `AskUserQuestion`. Looking like a default is not the same as being one |
| Genuinely blocked on access you can't grant | Prose explanation and stop | `AskUserQuestion`, then `needs input:` on its own line |

The escape hatch is not "ask in prose instead" — it is **don't ask**. Make the call, say which assumption you took, and continue. A background job's whole value is that it runs without supervision; a round-trip you didn't need costs more than a guess you can label.

**The escape hatch is bounded, and the bound is load-bearing.** It covers decisions that are scoped, low-risk, and reversible — those, and nothing else. It does not license deciding unscoped, irreversible, or security-sensitive questions just because the job is unattended and one option looks obvious. `CLAUDE.md` § Decision Engagement makes scoping the primary gate precisely because *stakes* is the seductive axis: a decision can be low-stakes and still be the user's to make, and running unsupervised makes a wrong autonomous call *harder* to catch, not easier. When the two readings conflict, the narrower one wins: ask.

## Subagents Without The Tool

A custom agent's `tools:` allowlist may exclude `AskUserQuestion` (e.g. `agents/context-fetcher.md`). Such an agent **cannot** comply with the rule above and must not try: anything it writes as a question is text its *caller* reads, not a prompt the user ever sees, so it would block or fail while looking like it asked.

The rule for those agents is a **handoff, not a question**: return the decision — options, distinguishing detail, and a recommendation — as structured output, flagged so the caller can spot it (`AMBIGUOUS:` or similar). The caller, which does have the tool, raises it. The obligation to surface the decision is unchanged; only who calls the tool moves. When writing a new custom agent, decide deliberately which of the two it is, and say so in its process section.

## Applies To The Whole Class

This is not only about literal questions. Any output whose purpose is to *elicit a response* — a confirmation request, an "OK to proceed?", a menu of options, a request to review before continuing — MUST be an `AskUserQuestion` call in a background job, or must not be emitted at all.

## Foreground Sessions

In an interactive session the user is present and prose questions do reach them, so `AskUserQuestion` is a formatting choice rather than a requirement. Prefer it anyway when the answer is a pick from a small option set: it records the choice as structured data and lets the user answer with one keystroke.

## Related

- The `grilling` skill's one-question-at-a-time discipline composes with this: one question at a time, each one an `AskUserQuestion` call
- `rules/workflow-defaults.md` § Config-First Responses — why this is a rule file and not a memory entry
