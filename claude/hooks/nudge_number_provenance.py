#!/usr/bin/env python3
"""F4 — substitution-class guard: numbers carry provenance.

Stop hook: if the turn is ending with numeric claims in the final message but
NO tool call was made anywhere in the turn, nudge. The failure mode this
guards: a number that was never measured gets stated with the same confidence
as one that was — the single most costly failure mode (confidently wrong
hallucination wastes real time).

Deliberately narrow: it fires only when zero tools ran, which is the case where
every number in the answer came from the model rather than from the system.
A turn that ran even one tool is left alone.

NEVER blocks. It only ever emits a systemMessage and exits 0 — it must not set
`decision`, which would force the model to continue and could loop.
"""

import json
import re
import sys
from collections import deque

MAX_LINES = 400  # bounded tail of the transcript

# Numeric claims worth provenance. Deliberately excludes bare integers.
CLAIM_RES = [
    re.compile(r"\b\d+(?:\.\d+)?\s?%"),                      # 42%, 3.5 %
    re.compile(r"(?<![\w.])~\s?\d"),                          # ~40
    re.compile(r"\b\d+(?:\.\d+)?\s?[x×]\b"),                  # 3x, 2.5x
    re.compile(
        r"\b\d[\d,]*(?:\.\d+)?\s?"
        r"(tokens?|ms|milliseconds?|seconds?|secs?|minutes?|hours?|"
        r"[KMGT]?B\b|bytes?|samples?|runs?|rows?|files?|lines?|"
        r"requests?|calls?|users?|commits?)",
        re.I,
    ),
    re.compile(r"\b\d+(?:\.\d+)?\s?(?:pp|percentage points)\b", re.I),
]

# Stripped before scanning: things that look numeric but aren't claims.
NOISE_RES = [
    re.compile(r"```.*?```", re.S),            # fenced code
    re.compile(r"`[^`\n]*`"),                  # inline code (incl. file:line refs)
    re.compile(r"https?://\S+"),               # URLs
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),      # ISO dates
    # Version numbers: v-prefixed, or ≥3 components. A bare two-part decimal
    # ("3.5") is NOT scrubbed — it may be the number of a claim like "3.5%".
    re.compile(r"\bv\d+\.\d+(\.\d+)*\b|\b\d+\.\d+(\.\d+)+\b"),
    re.compile(r"\S+\.\w{1,5}:\d+"),           # path.py:123
    re.compile(r"#\d+"),                       # PR/issue refs
]


def message_parts(entry: dict) -> list:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    # `[]` is valid JSON: it parses fine, then has no .get(). A Stop hook that
    # raises is an error shown to the user, so degrade to silence instead.
    if not isinstance(data, dict):
        sys.exit(0)

    if data.get("stop_hook_active"):
        sys.exit(0)

    path = data.get("transcript_path", "")
    if not path:
        sys.exit(0)

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            # deque(maxlen=...) keeps only the tail; readlines() would
            # materialize the entire transcript before slicing.
            lines = deque(fh, maxlen=MAX_LINES)
    except Exception:
        sys.exit(0)

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue

    # Find the last real user message (tool results also arrive as type "user").
    start = 0
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        if e.get("type") != "user":
            continue
        parts = message_parts(e)
        if any(p.get("type") == "text" for p in parts if isinstance(p, dict)):
            start = i + 1
            break

    turn = entries[start:]
    if not turn:
        sys.exit(0)

    # Any tool use in the turn → numbers could be measured. Leave it alone.
    for e in turn:
        for p in message_parts(e):
            if isinstance(p, dict) and p.get("type") == "tool_use":
                sys.exit(0)

    # Final assistant text of the turn.
    text = ""
    for e in reversed(turn):
        if e.get("type") != "assistant":
            continue
        chunks = [
            p.get("text", "")
            for p in message_parts(e)
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        if any(c.strip() for c in chunks):
            text = "\n".join(chunks)
            break

    if not text.strip():
        sys.exit(0)

    scrubbed = text
    for noise in NOISE_RES:
        scrubbed = noise.sub(" ", scrubbed)

    hits = []
    for claim in CLAIM_RES:
        for m in claim.finditer(scrubbed):
            hit = m.group(0).strip()
            if hit not in hits:
                hits.append(hit)
    if not hits:
        sys.exit(0)

    shown = ", ".join(hits[:4])
    print(json.dumps({
        "systemMessage": (
            f"NUDGE: this reply states figures ({shown}) but the turn ran no "
            "tools — so these numbers came from memory, not measurement. Say "
            "where each came from, or mark it as an estimate. Never give "
            "duration or cost estimates you have not actually calculated."
        )
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
