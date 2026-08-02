# Reusable Component Promotion

Part of a `/simplify` pass: throwaway scripts that got used repeatedly are refactored into permanent components, not left in `/tmp`. The `simplify_track_reuse.py` hook flags candidates at the third run; the same judgement applies whenever you notice a scratch script earning its keep.

## Promote when

Ran three or more times, at least once unchanged (it works — you're using it, not still writing it), and the next use is foreseeable. A script that ran twice while being debugged and then never again is doing its job as a throwaway; leave it.

## Where it goes

| Shape | Home |
|---|---|
| Command you'd want on PATH anywhere | `custom_bins/<name>` (`chmod +x`, no extension) |
| Repo-specific task | that repo's `scripts/` or `justfile` recipe |
| Shell one-liner or wrapper | `config/aliases/<topic>.sh` |
| Python helper importable by other code | the owning package, not a loose script |
| Multi-step procedure Claude reruns | a skill under `claude/skills/` |

## What promotion means

Copy-with-a-new-name is not promotion. Give it argument parsing instead of edit-the-constant-at-the-top, a `--help`, real exit codes, and no hardcoded absolute paths from the machine it was born on. Python past ~50 lines of shell (`coding-conventions.md`); `shellcheck` clean if it stays shell. Delete the scratch original once the permanent one works, and say where it went.

## Don't

Don't promote something the repo already has — search first; the point is fewer components, not more. Don't promote secrets or a session-specific path along with the logic. Don't build configuration for uses nobody has asked for yet: port what the script does today.
