#!/usr/bin/env python3
"""F2 — substitution-class guard: synthetic-data marker scan.

PostToolUse(Write|Edit): flag markers of fabricated data landing in
non-test code. The failure mode this guards: real data is unavailable or
awkward, so a plausible stand-in gets generated and the result is reported as
if measured. Zero-tolerance rule: mock data belongs in unit tests only; if data
is unavailable, ASK.

NUDGE only — never blocks, always exit 0. Synthetic data is legitimate in
plenty of places (tests, simulation, augmentation); the nudge exists to make an
implicit substitution explicit, not to veto it.
"""

import json
import re
import sys

# Paths where synthetic data is expected — no nudge.
SKIP_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|fixtures|node_modules|\.venv|__pycache__|tmp|archive|\.git)(/|$)"
    r"|(^|/)(test_[^/]*|conftest\.py|[^/]*_test\.(py|ts|js|rs))$"
)

SCAN_EXT_RE = re.compile(r"\.(py|ts|tsx|js|mjs|rs|R|ipynb)$")

# Markers, grouped so the nudge can say *what* it saw.
MARKERS = [
    (re.compile(r"\b(mock|dummy|fake|synthetic|placeholder|sample)_(data|df|results?|scores?|df|records?|rows?|responses?)\b", re.I),
     "a mock/dummy/synthetic data identifier"),
    (re.compile(r"\bnp\.random\.|numpy\.random\.|torch\.rand(n|int|_like)?\(|tf\.random\."),
     "a random-number generator producing data values"),
    (re.compile(r"\brandom\.(uniform|gauss|normal|randint|choice|sample)\("),
     "a random-number generator producing data values"),
    (re.compile(r"#\s*(TODO|FIXME|XXX)[^\n]*\b(real data|actual data|replace .*data)\b", re.I),
     "a TODO admitting the data is a stand-in"),
    (re.compile(r"\b(hardcoded|made[- ]up|for now,? (just )?(use|return))\b", re.I),
     "a comment describing a stand-in value"),
    (re.compile(r"\blorem ipsum\b", re.I),
     "placeholder text"),
]


def extract(data: dict) -> tuple[str, str]:
    inp = data.get("tool_input", data) or {}
    path = inp.get("file_path", "") or ""
    # Write carries `content`; Edit carries `new_string`; MultiEdit carries `edits`.
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

    seen = []
    for pattern, label in MARKERS:
        if pattern.search(content) and label not in seen:
            seen.append(label)
    if not seen:
        sys.exit(0)

    what = "; ".join(seen)
    print(json.dumps({
        "systemMessage": (
            f"NUDGE: this edit to {path.rsplit('/', 1)[-1]} contains {what}. "
            "Mock/synthetic data must not stand in for real data outside tests — "
            "if the real data is unavailable, say so and ask rather than "
            "generating a plausible substitute. If this IS legitimate "
            "(simulation, augmentation, a test fixture), ignore this."
        )
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
