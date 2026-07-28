# Verify Before Instructing

Before stating any specific claim the user could paste into a terminal or act on — CLI flags, URLs, version numbers, API signatures, comparative and pricing claims — verify it from the authoritative source. Confident wrongness is the failure mode; hedging ("I think X — check `tool --help`") is fine.

| Claim | Source |
|---|---|
| CLI flag / behavior | run `tool --help` / `man tool` |
| Library API | Context7, else official docs via WebFetch |
| Version / release | `tool --version`, package registry, GitHub releases |
| Pricing / plan features | vendor's own pricing page |
| URL resolves | `curl -I` or WebFetch |
| Comparative ("faster than") | cite a benchmark or drop it |

Verify in main context — never delegate a lookup to an agent. Diagnostic: an agent returning 0 tool_uses on a lookup task guessed.
