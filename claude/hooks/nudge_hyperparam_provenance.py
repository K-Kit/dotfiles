#!/usr/bin/env python3
"""F3 — substitution-class guard: hyperparameter provenance.

PostToolUse(Write|Edit): flag hyperparameters hardcoded as bare numeric
literals. The failure mode this guards: the correct value is in a config, a
paper, or a prior run, but a plausible-looking number gets typed in instead —
and the run then silently measures something other than the intended setup.

Does NOT fire when the value is already exposed as configuration
(`add_argument`, pydantic `Field`, `default=`, env lookup, `cfg.`/`config.`),
which is the desired pattern.

NUDGE only — never blocks, always exit 0.
"""

import json
import re
import sys

SKIP_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|node_modules|\.venv|__pycache__|tmp|archive|\.git)(/|$)"
    r"|(^|/)(test_[^/]*|conftest\.py|[^/]*_test\.(py|ts|js|rs))$"
)
SCAN_EXT_RE = re.compile(r"\.(py|ts|tsx|js|mjs|rs|ipynb)$")

# Deliberately ML/experiment-specific. `seed` is excluded: seed=42 is ubiquitous
# and its provenance genuinely does not matter — but the *number* of seeds,
# rollouts, or trials sets statistical power, so those are covered.
HYPERPARAMS = (
    "learning_rate|lr|batch_size|micro_batch_size|n_epochs|num_epochs|epochs|"
    "weight_decay|warmup_steps|warmup_ratio|max_steps|grad_accum|"
    "gradient_accumulation_steps|temperature|top_p|top_k|max_tokens|"
    "max_new_tokens|num_layers|n_layers|hidden_dim|hidden_size|n_heads|"
    "num_heads|dropout|beta1|beta2|clip_grad|max_grad_norm|lora_rank|lora_alpha|"
    # Eval-run parameters (LLM / AI-safety evals)
    "n_seeds|num_seeds|n_rollouts|num_rollouts|n_trajectories|num_trajectories|"
    "n_trials|num_trials|n_samples|num_samples|max_turns|max_messages"
)
# Optional type annotation (`n_rollouts: int = 16`, TS `max_turns: number = 5`)
# between the name and the assignment.
ASSIGN_RE = re.compile(
    rf"(?<![\w.])({HYPERPARAMS})\s*(?::\s*[A-Za-z_][\w.\[\], ]*?)?\s*[:=]\s*"
    rf"(-?\d+\.?\d*(?:[eE][-+]?\d+)?)\b"
)

# Judge/monitor/scorer prompts inlined as string literals: for evals the prompt
# IS a hyperparameter — it should come from a versioned file or config, not be
# typed in ad hoc where nothing records which version scored the run. Allows a
# type annotation, a wrapping paren, and f/r/b string prefixes.
PROMPT_ASSIGN_RE = re.compile(
    r"(?<![\w.])((?:judge|monitor|grader|scorer)_prompt)"
    r"(?:\s*:\s*\w+)?\s*[:=]\s*\(?\s*[frbuFRBU]*[\"']"
)

# The matched value is already routed through configuration — not a
# substitution. Checked against the match's own argument segment, not the
# whole line: `run_eval(model=cfg.model, n_rollouts=16)` still hardcodes 16.
EXEMPT_RE = re.compile(
    r"add_argument|Field\(|default\s*=|os\.environ|getenv|"
    r"\bcfg\.|\bconfig\.|\bargs\.|\bhparams\b|BaseSettings|@dataclass",
    re.I,
)


def strip_comment(line: str) -> str:
    """Drop `#`/`//` comments so commented-out params don't fire. Quote parity
    keeps `#` inside string literals (and `//` in quoted URLs) intact."""
    for i, ch in enumerate(line):
        if ch == "#" or (ch == "/" and line[i : i + 2] == "//"):
            before = line[:i]
            if before.count('"') % 2 == 0 and before.count("'") % 2 == 0:
                return line[:i]
    return line


def in_string(line: str, pos: int) -> bool:
    """True when pos sits inside a quoted literal (odd quotes before it) —
    `print("fallback n_samples=10")` is prose, not an assignment."""
    before = line[:pos]
    return before.count('"') % 2 == 1 or before.count("'") % 2 == 1


def exempt(line: str, start: int, end: int) -> bool:
    """Check the match's argument segment (nearest `,`/`(` boundaries)."""
    left = max(line.rfind(",", 0, start), line.rfind("(", 0, start)) + 1
    stops = [p for p in (line.find(",", end), line.find(")", end)) if p != -1]
    right = min(stops) if stops else len(line)
    return bool(EXEMPT_RE.search(line[left:right]))


def extract(data: object) -> tuple[str, str]:
    # A nudge must never crash: a non-zero exit from a PostToolUse hook is an
    # error surfaced to the user, and `[]` is valid JSON that parses fine and
    # then has no .get(). Degrade to "nothing to say", not to a traceback.
    if not isinstance(data, dict):
        return "", ""
    inp = data.get("tool_input", data)
    if not isinstance(inp, dict):
        return "", ""
    path = inp.get("file_path", "") or ""
    content = inp.get("content") or inp.get("new_string") or ""
    if not content and isinstance(inp.get("edits"), list):
        content = "\n".join(
            e.get("new_string", "") for e in inp["edits"] if isinstance(e, dict)
        )
    return path, content


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    path, content = extract(data)
    if not path or not content:
        sys.exit(0)
    if SKIP_PATH_RE.search(path) or not SCAN_EXT_RE.search(path):
        sys.exit(0)

    found = []
    for raw in content.splitlines():
        line = strip_comment(raw)
        for m in ASSIGN_RE.finditer(line):
            if in_string(line, m.start(2)) or exempt(line, m.start(), m.end()):
                continue
            item = f"{m.group(1)}={m.group(2)}"
            if item not in found:
                found.append(item)
        for m in PROMPT_ASSIGN_RE.finditer(line):
            if in_string(line, m.start(1)):
                continue
            item = f"{m.group(1)}=<inline literal>"
            if item not in found:
                found.append(item)

    if not found:
        sys.exit(0)

    shown = ", ".join(found[:4])
    more = f" (+{len(found) - 4} more)" if len(found) > 4 else ""
    print(json.dumps({
        "systemMessage": (
            f"NUDGE: hardcoded hyperparameter(s) in {path.rsplit('/', 1)[-1]}: "
            f"{shown}{more}. State where each value came from (paper, existing "
            "config, prior run) in a comment, or read it from config rather than "
            "picking a plausible number. Experiments should use the correct "
            "hyperparams from the validated setup, not invented ones."
        )
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
