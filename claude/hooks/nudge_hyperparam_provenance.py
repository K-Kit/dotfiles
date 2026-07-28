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
ASSIGN_RE = re.compile(
    rf"(?<![\w.])({HYPERPARAMS})\s*[:=]\s*(-?\d+\.?\d*(?:[eE][-+]?\d+)?)\b"
)

# Judge/monitor/scorer prompts inlined as string literals: for evals the prompt
# IS a hyperparameter — it should come from a versioned file or config, not be
# typed in ad hoc where nothing records which version scored the run.
PROMPT_ASSIGN_RE = re.compile(
    r"(?<![\w.])((?:judge|monitor|grader|scorer)_prompt)(?:\s*:\s*str)?\s*[:=]\s*[\"']"
)

# Lines already routing the value through configuration — not a substitution.
EXEMPT_LINE_RE = re.compile(
    r"add_argument|Field\(|default\s*=|os\.environ|getenv|"
    r"\bcfg\.|\bconfig\.|\bargs\.|\bhparams\b|BaseSettings|@dataclass",
    re.I,
)


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
    for line in content.splitlines():
        if EXEMPT_LINE_RE.search(line):
            continue
        for name, value in ASSIGN_RE.findall(line):
            item = f"{name}={value}"
            if item not in found:
                found.append(item)
        for name in PROMPT_ASSIGN_RE.findall(line):
            item = f"{name}=<inline literal>"
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
