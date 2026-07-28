#!/usr/bin/env bash
# Tests for the F1-F4 substitution-class guards.
#
# These four are NUDGES, not blocks, so the contract under test is different
# from block_*.sh: each must (a) actually emit a systemMessage on a positive
# case — a hook that never fires is indistinguishable from no hook at all —
# (b) stay silent on negatives, and (c) NEVER exit non-zero. A Stop hook that
# exits 2 or sets `decision` would loop the model, so F4's exit code is
# load-bearing, not incidental.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PASS=0
FAIL=0
# $TMPDIR is not reliably writable — sandboxes pin it to a read-only runtime dir
# (/run/user/$UID) and can mount /tmp read-only too, which would abort the suite
# before its first assertion and report an environment problem as a hook failure.
# Plain mkdir rather than mktemp: BSD mktemp lets $TMPDIR override -p, so the
# fallback would not actually fall back on macOS.
TMP=""
for cand in "${TMPDIR:-}" /tmp/claude /tmp .; do
    [ -n "$cand" ] || continue
    if mkdir -p "$cand/subst-guard-tests.$$" 2>/dev/null; then
        TMP="$cand/subst-guard-tests.$$"
        break
    fi
done
[ -n "$TMP" ] || { echo "no writable temp dir found" >&2; exit 1; }
trap 'rm -rf "$TMP"' EXIT

# run <desc> <hook> <json-input> <fire|silent>
run() {
    local desc="$1" hook="$2" input="$3" expect="$4"
    local out rc=0

    case "$hook" in
        *.py) out=$(printf '%s' "$input" | python3 "$DIR/$hook" 2>/dev/null) || rc=$? ;;
        *)    out=$(printf '%s' "$input" | bash "$DIR/$hook" 2>/dev/null) || rc=$? ;;
    esac

    if [ "$rc" -ne 0 ]; then
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (nudge hook exited %d — must always be 0)\n' "$desc" "$rc"
        return
    fi

    local fired=silent
    case "$out" in *systemMessage*) fired=fire ;; esac

    if [ "$fired" = "$expect" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s (expected %s, got %s)\n' "$desc" "$expect" "$fired"
    fi
}

# Values are passed via argv, never interpolated into source — paths and code
# snippets under test contain quotes and backslashes.
tool_json() {
    python3 -c "
import json, sys
tool, path, key, value = sys.argv[1:5]
print(json.dumps({'tool_name': tool, 'tool_input': {'file_path': path, key: value}}))
" "$1" "$2" "$3" "$4"
}

pre_write() { tool_json Write "$1" content "$2"; }
post_edit() { tool_json Edit "$1" new_string "$2"; }

stop_json() {
    python3 -c "
import json, sys
print(json.dumps({'transcript_path': sys.argv[1],
                  'stop_hook_active': sys.argv[2] == 'true'}))
" "$1" "${2:-false}"
}

# --- F1: guard_existing_code.sh (new source file) ----------------------------
# Fixture paths are SYNTHETIC (/repo/...), not under $TMP: $TMPDIR is /tmp/claude,
# and all of F1-F3 skip any path matching `(^|/)tmp(/|$)`, so real temp paths
# would silence every positive case and the suite would pass vacuously.
# F1 only needs the path to NOT exist, which a synthetic path satisfies.
echo "=== F1: new-source-file guard ==="
EXISTING="$TMP/already_here.py"          # the one case that needs a real file
printf 'x = 1\n' > "$EXISTING"

run "new .py file"        guard_existing_code.sh "$(pre_write /repo/src/run_experiment.py "print(1)")" fire
run "new .ts file"        guard_existing_code.sh "$(pre_write /repo/src/client.ts "export const a=1")" fire
run "existing file"       guard_existing_code.sh "$(pre_write "$EXISTING" "x = 2")"        silent
run "new test file"       guard_existing_code.sh "$(pre_write /repo/test_thing.py "x")"    silent
run "new markdown"        guard_existing_code.sh "$(pre_write /repo/notes.md "hi")"        silent
run "new file under tmp/" guard_existing_code.sh "$(pre_write /repo/tmp/scratch.py "x")"   silent
run "no file_path"        guard_existing_code.sh '{"tool_name":"Write","tool_input":{}}'   silent

# --- F2: nudge_synthetic_data.py ---------------------------------------------
echo "=== F2: synthetic-data markers ==="
run "mock_data identifier" nudge_synthetic_data.py \
    "$(pre_write /repo/src/analysis.py "mock_data = [1, 2, 3]")" fire
run "np.random values" nudge_synthetic_data.py \
    "$(pre_write /repo/src/analysis.py "scores = np.random.normal(0, 1, 100)")" fire
# AC11a names this exact literal as F2's triggering action. Kept as its own case
# so the criterion is proven on its own wording, not on a near-neighbour.
run "AC11a literal: np.random.randn(100)" nudge_synthetic_data.py \
    "$(pre_write /repo/src/analysis.py "X = np.random.randn(100)")" fire
run "torch.randn" nudge_synthetic_data.py \
    "$(pre_write /repo/src/model.py "batch = torch.randn(8, 512)")" fire
run "TODO real data" nudge_synthetic_data.py \
    "$(pre_write /repo/src/load.py "# TODO: replace with real data")" fire
run "lorem ipsum" nudge_synthetic_data.py \
    "$(pre_write /repo/src/page.ts "const body = 'Lorem ipsum dolor'")" fire
run "Edit new_string" nudge_synthetic_data.py \
    "$(post_edit /repo/src/analysis.py "fake_results = {}")" fire
run "clean code" nudge_synthetic_data.py \
    "$(pre_write /repo/src/analysis.py "df = load_eval_log(path)")" silent
run "markers in test file" nudge_synthetic_data.py \
    "$(pre_write /repo/src/test_analysis.py "mock_data = [1]")" silent
run "markers in tests/ dir" nudge_synthetic_data.py \
    "$(pre_write /repo/tests/helpers.py "mock_data = [1]")" silent
run "non-source extension" nudge_synthetic_data.py \
    "$(pre_write /repo/notes.md "mock_data everywhere")" silent

# --- F3: nudge_hyperparam_provenance.py --------------------------------------
echo "=== F3: hyperparameter provenance ==="
run "bare learning_rate" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/train.py "learning_rate = 3e-4")" fire
run "bare batch_size" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/train.py "batch_size = 32")" fire
run "dict-style temperature" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/gen.py "cfg = dict(temperature=0.7)")" fire
run "via add_argument" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/train.py "p.add_argument('--lr', default=3e-4)")" silent
run "via pydantic Field" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/conf.py "learning_rate: float = Field(3e-4)")" silent
run "read from config" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/train.py "batch_size = cfg.batch_size")" silent
run "eval n_rollouts" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/eval.py "n_rollouts = 16")" fire
run "eval num_seeds" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/eval.py "num_seeds = 3")" fire
run "eval max_turns" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/eval.py "max_turns = 30")" fire
run "inline judge_prompt literal" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/eval.py "judge_prompt = 'Rate the response 1-10'")" fire
run "inline monitor_prompt literal" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/eval.py "monitor_prompt: str = \"Watch for deception\"")" fire
run "judge_prompt from file" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/eval.py "judge_prompt = load_prompt(path)")" silent
run "judge_prompt from config" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/eval.py "judge_prompt = cfg.judge_prompt")" silent
run "seed excluded" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/train.py "seed = 42")" silent
run "no hyperparams" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/train.py "total = 5")" silent
run "hyperparams in test" nudge_hyperparam_provenance.py \
    "$(pre_write /repo/test_train.py "learning_rate = 3e-4")" silent

# --- F4: nudge_number_provenance.py (Stop hook) ------------------------------
echo "=== F4: number provenance (Stop) ==="

# Build a transcript: one user text turn, then assistant content.
# with_tool=1 inserts a tool_use block, which must suppress the nudge.
transcript() {
    local file="$1" reply="$2" with_tool="${3:-0}"
    python3 - "$file" "$reply" "$with_tool" <<'PY'
import json, sys
path, reply, with_tool = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
rows = [{"type": "user", "message": {"content": [{"type": "text", "text": "how big is it?"}]}}]
if with_tool:
    rows.append({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "wc -c f"}}]}})
    rows.append({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "1234"}]}})
rows.append({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}})
with open(path, "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")
PY
}

stop_input() { stop_json "$1"; }

transcript "$TMP/t_pct.jsonl"   "That cut it by 43% overall."
transcript "$TMP/t_unit.jsonl"  "The file is about 4200 tokens."
transcript "$TMP/t_approx.jsonl" "It takes ~40 seconds to run."
transcript "$TMP/t_tool.jsonl"  "That cut it by 43% overall." 1
transcript "$TMP/t_clean.jsonl" "Done — the hook is wired and the suite passes."
transcript "$TMP/t_code.jsonl"  'Set it with `--limit 50%` in the config.'
transcript "$TMP/t_ver.jsonl"   "Requires uv 0.11.16 or newer."

run "percentage, no tools"  nudge_number_provenance.py "$(stop_input "$TMP/t_pct.jsonl")"   fire
run "token count, no tools" nudge_number_provenance.py "$(stop_input "$TMP/t_unit.jsonl")"  fire
run "~N seconds, no tools"  nudge_number_provenance.py "$(stop_input "$TMP/t_approx.jsonl")" fire
run "same claim WITH tool"  nudge_number_provenance.py "$(stop_input "$TMP/t_tool.jsonl")"  silent
run "no numeric claims"     nudge_number_provenance.py "$(stop_input "$TMP/t_clean.jsonl")" silent
run "number in inline code" nudge_number_provenance.py "$(stop_input "$TMP/t_code.jsonl")"  silent
run "version number only"   nudge_number_provenance.py "$(stop_input "$TMP/t_ver.jsonl")"   silent
run "stop_hook_active set"  nudge_number_provenance.py \
    "$(stop_json "$TMP/t_pct.jsonl" true)" silent
run "missing transcript"    nudge_number_provenance.py \
    '{"transcript_path":"/nonexistent/nope.jsonl"}' silent
run "malformed input"       nudge_number_provenance.py 'not json at all' silent

# --- Regression: valid JSON that is not an object -----------------------------
# `[]` parses cleanly and then has no .get(), so the natural code raises. For a
# PostToolUse or Stop hook a traceback is not a silent no-op — the non-zero exit
# surfaces to the user as a hook error on every tool call. `run` already fails
# any non-zero exit, so these assert the degradation is to SILENCE, not a crash.
# `malformed input` above only covers unparseable text, which takes a different
# branch; that test passing is not evidence for this one.
echo "=== Regression: non-dict JSON payloads ==="
for h in guard_existing_code.sh nudge_synthetic_data.py \
         nudge_hyperparam_provenance.py nudge_number_provenance.py; do
    run "$h on []"      "$h" '[]'      silent
    run "$h on a string" "$h" '"hi"'   silent
    run "$h on null"    "$h" 'null'    silent
done

echo ""
echo "Results: $PASS passed, $FAIL failed (total $((PASS + FAIL)))"
[ "$FAIL" -eq 0 ] && echo "All tests passed!" || exit 1
