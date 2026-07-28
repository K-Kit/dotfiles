#!/usr/bin/env python3
"""Markdown hard-wrap nudge — enforces markdown-style.md's "one paragraph =
one line" at write time.

PostToolUse(Write|Edit) on `.md` files: flag prose paragraphs broken by hard
newlines. Only the written fragment is scanned (Write content / Edit
new_string), so editing near pre-existing hard wraps doesn't re-flag them.

The heuristic is deliberately conservative — a line that ends mid-sentence
(lowercase letter or comma) followed by a line starting lowercase, outside
fenced code blocks and non-prose constructs. Missing a wrap is cheaper than a
nudge nobody trusts.

NUDGE only — never blocks, always exit 0.
"""

import json
import re
import sys

SKIP_PATH_RE = re.compile(r"(^|/)(node_modules|\.venv|\.git|archive)(/|$)")

# Lines that are not flowing prose: headings, list items, quotes, tables,
# fences, HTML, footnotes/link defs, YAML-ish keys, indented code.
NON_PROSE_RE = re.compile(
    r"^\s*(#|[-*+]\s|\d+[.)]\s|>|\||```|~~~|<|\[\^|\[[^\]]+\]:|\S+:\s|    )"
)
ENDS_MID_SENTENCE_RE = re.compile(r"[a-z,]$")
STARTS_LOWER_RE = re.compile(r"^[a-z(\"'`]")


def extract(data: object) -> tuple[str, str]:
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


def find_hard_wraps(content: str) -> list[str]:
    hits = []
    in_fence = False
    lines = content.splitlines()
    for line, nxt in zip(lines, lines[1:]):
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if NON_PROSE_RE.match(line) or not nxt.strip():
            continue
        if NON_PROSE_RE.match(nxt):
            continue
        if (
            len(line.strip()) > 40
            and ENDS_MID_SENTENCE_RE.search(line.rstrip())
            and STARTS_LOWER_RE.match(nxt.strip())
        ):
            hits.append(line.strip()[:60])
    return hits


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    path, content = extract(data)
    if not path or not content or not path.endswith(".md"):
        sys.exit(0)
    if SKIP_PATH_RE.search(path):
        sys.exit(0)

    hits = find_hard_wraps(content)
    if not hits:
        sys.exit(0)

    more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
    print(json.dumps({
        "systemMessage": (
            f"NUDGE: hard-wrapped paragraph(s) in {path.rsplit('/', 1)[-1]}, "
            f"e.g. “{hits[0]}…”{more}. One paragraph = one line "
            "— join the lines; blank lines separate paragraphs "
            "(markdown-style.md)."
        )
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
