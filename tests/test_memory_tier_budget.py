"""Acceptance verification for the memory-tier trim (AC1, AC4)."""

import subprocess

TAG = "pretrim-memory-2026-07-29"
FILES = [
    ("CLAUDE.md", 24223, 7500),
    ("claude/CLAUDE.md", 5911, 3800),
    ("claude/rules/background-job-questions.md", 4176, 900),
    ("claude/rules/safety-and-git.md", 2558, 1750),
    ("claude/rules/coding-conventions.md", 2130, 1300),
]


def tagged(path: str) -> str:
    out = subprocess.run(
        ["git", "show", f"{TAG}:{path}"], capture_output=True, check=True
    )
    return out.stdout.decode()


def main() -> None:
    print("=== AC1: byte ceilings (wc -c) ===")
    tb = tn = tc = 0
    fails = []
    for path, base, ceil in FILES:
        n = len(open(path, "rb").read())
        tb, tn, tc = tb + base, tn + n, tc + ceil
        if n > ceil:
            fails.append((path, n, ceil))
        print(f"{path:<45} {base:6d} -> {n:5d}  ceiling {ceil:5d}  {'OK' if n <= ceil else 'OVER'}")
    print(f"{'TOTAL':<45} {tb:6d} -> {tn:5d}  ceiling {tc:5d}")
    print(f"reduction: {tb - tn} bytes ({100 * (tb - tn) / tb:.0f}%)")
    for path, n, ceil in fails:
        print(f"AC1 FAIL: {path} is {n - ceil} bytes over")
    if not fails:
        print("AC1 PASS")

    print("\n=== AC4: protected passages vs tag ===")
    old = tagged("claude/CLAUDE.md")
    new = open("claude/CLAUDE.md").read()
    for name, key in [
        ("bright-red-lines", "IMPORTANT NOTE"),
        ("use-existing-code", "Use existing code"),
    ]:
        o = [ln for ln in old.splitlines() if key in ln]
        n = [ln for ln in new.splitlines() if key in ln]
        same = bool(o) and bool(n) and o[0] == n[0]
        print(f"{name:<20} {'IDENTICAL' if same else 'DIFFERS'}")
        if not same:
            print("  tag:", o[:1])
            print("  now:", n[:1])

    marker = "## Sandbox failure modes"
    o_tab = tagged("claude/rules/safety-and-git.md")
    n_tab = open("claude/rules/safety-and-git.md").read()
    o_tab = o_tab[o_tab.index(marker):]
    n_tab = n_tab[n_tab.index(marker):]
    print(f"{'sandbox table':<20} {'IDENTICAL' if o_tab == n_tab else 'DIFFERS'} ({len(n_tab)} bytes)")


if __name__ == "__main__":
    main()
