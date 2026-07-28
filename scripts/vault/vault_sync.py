#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Vault sync quota remediation tool.

Implements the execution plan in ~/vault/specs/2026-07-27-vault-sync-execution.md.
Subcommands map one-to-one onto the spec's steps:

    gate      EX-2   read-only state.db sanity checks + positive control
    stage     EX-3   compute the deletion set (twin-verified, per file)
              EX-4   move it out of the vault with mv, never rm
    sync      EX-5   bidirectional sync so the deletions reach the server
    verify    EX-6   read tombstones from server_files, split by folder
    filters   EX-7   the single sync-config call that tightens the filters
    tripwire  EX-9   detect regenerable dirs and oversized files

Ordering is load-bearing. Exclusion is non-retroactive: tightening the filters
before the deletions have synced strands the remote copies permanently. `filters`
therefore refuses to run without a recorded approval, and `verify` hard-stops on a
tombstone count of zero.

EX-10: encryptionKey and encryptionSalt must never reach a terminal, a log or a
commit. Config printing is allowlisted by key name -- see SAFE_CONFIG_KEYS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path("/home/yulong/vault")
RUNS_DIR = VAULT / "research/monitorability/runs"
OUT_DIR = Path("/home/yulong/projects/nla-vs-cot/out")
SYNC_ROOT = Path.home() / ".config/obsidian-headless/sync"

# Vault-relative, no leading or trailing slash. A trailing slash is a silent
# no-op in the ob matcher, which compares `path == folder or path.startswith(folder + "/")`.
EXCLUDED_FOLDER = "research/monitorability/slides/slidev/node_modules"
NODE_MODULES = VAULT / EXCLUDED_FOLDER

# EX-7: exactly these two flags, and --file-types is never the empty string.
# Passing "" does not clear the setting -- it drops allowTypes and falls back to
# the built-in default of image,audio,pdf,video, which is wider than we want.
FILE_TYPES = "image,pdf"

DEFAULT_HOLDING_ROOT = Path("/home/yulong/scratch")

# This file has exactly one copy. It is untracked in git, absent from out/, and
# over the per-file service cap so it was never on the remote. It must never
# enter the deletion set; the twin check already excludes it, this is the belt.
PROTECTED = {
    VAULT
    / "research/monitorability/runs/2026-07-23_jlens-judge-input-output-review"
    / "report_judge_input_output_jlens_n150x2.html",
}

# EX-10. Anything not named here is withheld, including keys added by future
# versions of the CLI -- the allowlist fails closed.
SAFE_CONFIG_KEYS = frozenset(
    {
        "vaultId",
        "vaultName",
        "vaultPath",
        "deviceName",
        "conflictStrategy",
        "allowTypes",
        "allowSpecialFiles",
        "ignoreFolders",
        "syncConfigs",
        "configDir",
        "syncMode",
        "host",
        "encryptionVersion",
        "version",
    }
)

# The CLI flag is --excluded-folders, but the key persisted to config.json and
# read back by the runtime matcher is `ignoreFolders`:
#     t.ignoreFolders = s.excludedFolders.split(",").map(n => n.trim())
#     _allowSyncFile(e, t) { for (let r of this.ignoreFolders) ... }
# Reading `excludedFolders` back always yields nothing, which would make the
# EX-7 post-condition fail on a successful write and leave the EX-9 tripwire
# permanently blind. Verified against obsidian-headless/cli.js on 2026-07-28.
EXCLUSION_KEY = "ignoreFolders"

# EX-2 baseline, measured 2026-07-27 against the untouched state.db.
BASELINE = {
    "server_rows": 27644,
    "local_rows": 27691,
    "server_with_deleted_key": 27644,
    "local_with_deleted_key": 0,
    "node_modules_rows": 26984,
    "tombstones": 0,
}

# Directories that regenerate from a lockfile or a build step and should never
# occupy sync quota. Checked by the tripwire against the live exclusion list.
REGENERABLE_DIRS = (
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    ".turbo",
    "dist",
    "build",
    "target",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
)

# The Obsidian Sync per-file cap. Files above it never reach the remote at all,
# so they are a silent-failure class, not a quota class.
DEFAULT_MAX_FILE_MB = 5.00

MB = 1024 * 1024

# `deleted` lives inside the data JSON blob, not in a column. json_type
# distinguishes an absent key from a present-but-false one, which is the whole
# point of check (a): local_files carries no `deleted` key at all, so a counter
# reading that table is structurally always zero (the QR-6 defect).
HAS_DELETED_KEY = "json_type(data, '$.deleted') IS NOT NULL"
IS_TOMBSTONE = "COALESCE(json_extract(data, '$.deleted'), 0) NOT IN (0, 'false')"
IS_FOLDER = "COALESCE(json_extract(data, '$.folder'), 0) NOT IN (0, 'false')"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    log()
    log(f"== {title} " + "=" * max(0, 66 - len(title)))


def fmt_mb(n_bytes: int) -> str:
    return f"{n_bytes / MB:.2f} MB"


def die(msg: str, clean: bool = True) -> None:
    """Abort. `clean=False` when a mutating call may already have landed.

    The reassurance must be a parameter, never a constant: a post-condition
    check runs *after* its mutation, so hardcoding "nothing changed" there
    tells the operator the opposite of the truth at the one moment it matters.
    """
    log()
    log(f"ABORT: {msg}")
    if clean:
        log("No filesystem change was made by this step.")
    else:
        log("WARNING: a mutating call already ran -- do NOT assume this step "
            "was a no-op. Inspect the live state before retrying.")
    raise SystemExit(1)


# --------------------------------------------------------------------------
# sync state discovery
# --------------------------------------------------------------------------


def find_state_dir() -> Path:
    if not SYNC_ROOT.is_dir():
        die(f"sync state root not found: {SYNC_ROOT}")
    candidates = [d for d in sorted(SYNC_ROOT.iterdir()) if (d / "state.db").is_file()]
    if not candidates:
        die(f"no vault state directory with a state.db under {SYNC_ROOT}")
    if len(candidates) > 1:
        die(
            "multiple vault state directories found, refusing to guess: "
            + ", ".join(d.name for d in candidates)
        )
    return candidates[0]


def read_config(state_dir: Path) -> dict:
    path = state_dir / "config.json"
    if not path.is_file():
        die(f"config.json not found at {path}")
    return json.loads(path.read_text())


def print_config_safely(cfg: dict) -> None:
    """EX-10: allowlist by key name, never dump the object."""
    shown = 0
    for key in sorted(cfg):
        if key in SAFE_CONFIG_KEYS:
            log(f"    {key}: {cfg[key]!r}")
            shown += 1
    withheld = len(cfg) - shown
    if withheld:
        log(f"    ({withheld} further key(s) withheld -- may contain secret material)")


def copy_state_db(state_dir: Path, dest_dir: Path) -> Path:
    """Copy state.db plus any -wal/-shm sidecars.

    A sync killed by SIGTERM never checkpoints, so fresh tombstones sit in the
    WAL rather than the main database file. Reading state.db alone would miss
    them entirely. The sidecars are frequently absent on a cleanly checkpointed
    database -- that is the normal case, not an error.
    """
    src = state_dir / "state.db"
    dest = dest_dir / "state.db"
    shutil.copy2(src, dest)
    for suffix in ("-wal", "-shm"):
        sidecar = state_dir / f"state.db{suffix}"
        if sidecar.exists():
            shutil.copy2(sidecar, dest_dir / f"state.db{suffix}")
            log(f"    copied sidecar state.db{suffix} ({fmt_mb(sidecar.stat().st_size)})")
    return dest


def open_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


# --------------------------------------------------------------------------
# EX-2: gate
# --------------------------------------------------------------------------


def cmd_gate(args: argparse.Namespace) -> int:
    state_dir = find_state_dir()
    rule("EX-2  read-only gate")
    log(f"  state dir: {state_dir}")

    with tempfile.TemporaryDirectory(prefix="vault-sync-gate-") as tmp:
        db = copy_state_db(state_dir, Path(tmp))
        conn = open_ro(db)

        server_rows = scalar(conn, "SELECT COUNT(*) FROM server_files")
        local_rows = scalar(conn, "SELECT COUNT(*) FROM local_files")

        # (a) local_files carries no `deleted` key -- the defective oracle.
        a = scalar(conn, f"SELECT COUNT(*) FROM local_files WHERE {HAS_DELETED_KEY}")
        # (b) server_files carries it on every row -- the correct oracle.
        b = scalar(conn, f"SELECT COUNT(*) FROM server_files WHERE {HAS_DELETED_KEY}")
        # (c) positive control: same machinery, predicate inverted, scoped to the
        #     node_modules prefix. If this returns 0 the JSON extraction is broken
        #     and check (d)'s zero would be meaningless.
        prefix = (EXCLUDED_FOLDER, EXCLUDED_FOLDER + "/%")
        c = scalar(
            conn,
            f"SELECT COUNT(*) FROM server_files "
            f"WHERE (path = ? OR path LIKE ?) AND NOT {IS_TOMBSTONE}",
            prefix,
        )
        # (d) the counter itself.
        d = scalar(conn, f"SELECT COUNT(*) FROM server_files WHERE {IS_TOMBSTONE}")
        conn.close()

    log()
    log(f"  server_files rows                          : {server_rows:>8,}")
    log(f"  local_files  rows                          : {local_rows:>8,}")
    log(f"  (a) local_files  rows with `deleted` key   : {a:>8,}   expect 0")
    log(f"  (b) server_files rows with `deleted` key   : {b:>8,}   expect == server rows")
    log(f"  (c) positive control, node_modules prefix  : {c:>8,}   expect > 0")
    log(f"  (d) tombstones vault-wide                  : {d:>8,}   expect 0 pre-deletion")

    failures: list[str] = []
    if a != 0:
        failures.append(f"(a) expected 0, got {a} -- local_files unexpectedly carries `deleted`")
    if b != server_rows:
        failures.append(f"(b) expected {server_rows}, got {b} -- not every server row carries the key")
    if c == 0:
        failures.append("(c) positive control returned 0 -- the JSON predicate is broken (EC-2)")
    if d != 0:
        failures.append(f"(d) expected 0 tombstones pre-deletion, got {d}")

    if failures:
        log()
        for f in failures:
            log(f"  FAIL {f}")
        die("structural gate failed")

    drift = []
    for label, got, want in (
        ("server_files rows", server_rows, BASELINE["server_rows"]),
        ("local_files rows", local_rows, BASELINE["local_rows"]),
        ("node_modules prefix rows", c, BASELINE["node_modules_rows"]),
    ):
        if got != want:
            drift.append(f"{label}: baseline {want:,}, now {got:,} (delta {got - want:+,})")

    if drift:
        log()
        log("  Counts have drifted from the 2026-07-27 baseline:")
        for line in drift:
            log(f"    - {line}")
        if not args.allow_drift:
            die("count drift from baseline; re-read the spec's measured state, then --allow-drift")
        log("  --allow-drift set, continuing.")

    log()
    log("  GATE PASSED. The counter reads 0 against a working query.")
    return 0


# --------------------------------------------------------------------------
# EX-3 / EX-4: stage
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    path: Path
    size: int
    twin: Path | None = None
    reason: str = ""


@dataclass
class StagePlan:
    eligible: list[Candidate] = field(default_factory=list)
    ineligible: list[Candidate] = field(default_factory=list)

    @property
    def eligible_bytes(self) -> int:
        return sum(c.size for c in self.eligible)

    @property
    def ineligible_bytes(self) -> int:
        return sum(c.size for c in self.ineligible)


def file_digest(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def identical(a: Path, b: Path) -> bool:
    """Byte-identical, confirmed at run time by content.

    Name and size are screens, not evidence -- size equality is checked first
    only because it is free and rejects most non-twins without hashing.
    """
    if a.stat().st_size != b.stat().st_size:
        return False
    return file_digest(a) == file_digest(b)


def compute_runs_plan() -> StagePlan:
    """EX-3: every file under runs/, classified by whether a twin exists now.

    The 144.61 MB / 25-file figure from the prior session is context. It is never
    an input -- the set is recomputed live, per file, on every run.
    """
    plan = StagePlan()
    if not RUNS_DIR.is_dir():
        die(f"runs directory not found: {RUNS_DIR}")
    if not OUT_DIR.is_dir():
        die(f"twin source not found: {OUT_DIR}")

    for path in sorted(RUNS_DIR.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        size = path.stat().st_size
        if path in PROTECTED:
            plan.ineligible.append(Candidate(path, size, reason="protected: single known copy"))
            continue
        twin = OUT_DIR / path.relative_to(RUNS_DIR)
        if not twin.is_file():
            plan.ineligible.append(Candidate(path, size, reason="no twin in out/"))
            continue
        # A symlinked or hardlinked "twin" is not an independent second copy.
        # A symlink pointing back into the vault would compare byte-identical
        # against itself, and moving the target would leave a dangling link and
        # no surviving copy at all. Reject both; Decision 2's "two copies on the
        # same disk" premise requires two real ones.
        if twin.is_symlink():
            plan.ineligible.append(
                Candidate(path, size, twin, "twin is a symlink, not an independent copy")
            )
            continue
        if path.samefile(twin):
            plan.ineligible.append(
                Candidate(path, size, twin, "twin is the same inode (hardlink), not a second copy")
            )
            continue
        if not identical(path, twin):
            plan.ineligible.append(Candidate(path, size, twin, "twin differs in content"))
            continue
        plan.eligible.append(Candidate(path, size, twin, "byte-identical twin confirmed"))

    return plan


def move_out(path: Path, holding: Path, base: Path) -> Path:
    """EX-4: mv, never rm. Structure is preserved so the move is reversible."""
    dest = holding / path.relative_to(base)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # shutil.move onto an existing file silently renames over it on POSIX, and
    # onto an existing directory moves the source *inside* it -- either way the
    # manifest would then record a destination that does not describe reality.
    if dest.exists() or dest.is_symlink():
        die(f"refusing to move onto an existing path: {dest}")
    shutil.move(str(path), str(dest))
    return dest


def cmd_stage(args: argparse.Namespace) -> int:
    rule("EX-3  deletion set, computed live")

    do_runs = args.what in ("runs", "both")
    do_nm = args.what in ("node-modules", "both")
    # Only hash when the runs/ scope is actually selected -- otherwise
    # `--what node-modules` pays ~126 MB of hashing on both sides for nothing,
    # and dies if OUT_DIR happens to be absent.
    plan = compute_runs_plan() if do_runs else StagePlan()

    if do_runs:
        log(f"  runs/ scanned under {RUNS_DIR}")
        log(f"  eligible   : {len(plan.eligible):>4} files  {fmt_mb(plan.eligible_bytes):>12}")
        log(f"  ineligible : {len(plan.ineligible):>4} files  {fmt_mb(plan.ineligible_bytes):>12}")

        if plan.eligible:
            log()
            log("  Eligible (byte-identical twin confirmed in out/):")
            for c in sorted(plan.eligible, key=lambda x: -x.size):
                log(f"    {fmt_mb(c.size):>10}  {c.path.relative_to(VAULT)}")

        # The residual the dropped 02b step would have handled. Surfaced in full
        # for the EX-11 review -- these are the files that stay in the vault.
        log()
        log("  Ineligible (stays in the vault):")
        for c in sorted(plan.ineligible, key=lambda x: -x.size):
            log(f"    {fmt_mb(c.size):>10}  {c.path.relative_to(VAULT)}")
            log(f"                {c.reason}")

    nm_bytes = 0
    if do_nm:
        if NODE_MODULES.is_dir():
            nm_bytes = sum(
                p.stat().st_size for p in NODE_MODULES.rglob("*") if p.is_file() and not p.is_symlink()
            )
            log()
            log(f"  node_modules: {EXCLUDED_FOLDER}")
            log(f"    {fmt_mb(nm_bytes)} on disk, regenerable from the lockfile")
        else:
            log()
            log(f"  node_modules already absent at {NODE_MODULES}")
            do_nm = False

    total = (plan.eligible_bytes if do_runs else 0) + nm_bytes
    log()
    log(f"  TOTAL TO MOVE: {fmt_mb(total)}")

    if not args.execute:
        log()
        log("  Dry run. Re-run with --execute to move (mv, not rm).")
        return 0

    rule("EX-4  move out of the vault")
    holding = Path(args.holding) if args.holding else DEFAULT_HOLDING_ROOT / f"vault-sync-holding-{utc_stamp()}"
    holding = holding.resolve()

    # EX-4 says "outside the vault". A holding dir inside it would satisfy "the
    # files moved" while defeating the entire point: nothing leaves the sync
    # scope, the sync emits no deletions, and verify hard-stops on zero.
    if holding == VAULT or VAULT in holding.parents:
        die(f"holding directory must be outside the vault, got {holding}")
    # Requiring a fresh directory closes the whole destination-collision class
    # in one line, rather than defending each move site separately.
    if holding.exists() and any(holding.iterdir()):
        die(f"holding directory already exists and is not empty: {holding}")
    holding.mkdir(parents=True, exist_ok=True)
    log(f"  holding: {holding}")

    manifest: list[dict] = []
    moved_bytes = 0

    # Append-as-we-go, so a crash mid-move (ENOSPC, SIGINT) still leaves a
    # record of every file already moved. manifest.json at the end is the
    # summary, not the only copy.
    journal_path = holding / "manifest.jsonl"

    def record(entry: dict) -> None:
        manifest.append(entry)
        with journal_path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    if do_runs:
        for c in plan.eligible:
            dest = move_out(c.path, holding / "runs", RUNS_DIR)
            record(
                {
                    "kind": "runs-duplicate",
                    "from": str(c.path),
                    "to": str(dest),
                    "twin": str(c.twin),
                    "size": c.size,
                }
            )
            moved_bytes += c.size
        log(f"  moved {len(plan.eligible)} run files")

    if do_nm:
        dest = holding / "node_modules"
        if dest.exists() or dest.is_symlink():
            die(f"refusing to move onto an existing path: {dest}")
        shutil.move(str(NODE_MODULES), str(dest))
        record(
            {"kind": "node-modules", "from": str(NODE_MODULES), "to": str(dest), "size": nm_bytes}
        )
        moved_bytes += nm_bytes
        log(f"  moved node_modules ({fmt_mb(nm_bytes)})")

    manifest_path = holding / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"created": utc_stamp(), "vault": str(VAULT), "moved_bytes": moved_bytes, "entries": manifest},
            indent=2,
        )
    )
    log()
    log(f"  {fmt_mb(moved_bytes)} moved. Manifest: {manifest_path}")
    log("  Reversible until the sync runs. Next: `sync`.")
    return 0


# --------------------------------------------------------------------------
# EX-5: sync
# --------------------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    state_dir = find_state_dir()
    cfg = read_config(state_dir)

    rule("EX-5  bidirectional sync")

    # The deletions must propagate. A pull-only or mirror-remote mode would pull
    # the files straight back instead. Absence of the key is the bidirectional
    # default -- the tool asserts absence rather than setting a mode, because
    # setting one would persist it into config.json.
    mode = cfg.get("syncMode")
    if mode is None:
        log("  syncMode key absent -> bidirectional default. Not setting a mode.")
    elif mode == "bidirectional":
        log("  syncMode is explicitly 'bidirectional'. Not changing it.")
    else:
        die(f"syncMode is {mode!r}; deletions would not propagate. Fix before syncing.")

    cmd = ["ob", "sync", "--path", str(VAULT)]
    log(f"  $ {' '.join(cmd)}")
    if args.dry_run:
        log("  Dry run, not executing.")
        return 0

    # Deliberately no timeout. SIGTERM never checkpoints the WAL, which strands
    # fresh tombstones outside state.db and makes EX-6 read a stale zero.
    log()
    proc = subprocess.run(cmd, cwd=str(VAULT))
    log()
    if proc.returncode != 0:
        die(f"ob sync exited {proc.returncode}")
    log("  Sync completed. Next: `verify`.")
    return 0


# --------------------------------------------------------------------------
# EX-6: verify
# --------------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    state_dir = find_state_dir()
    rule("EX-6  tombstone read")

    with tempfile.TemporaryDirectory(prefix="vault-sync-verify-") as tmp:
        db = copy_state_db(state_dir, Path(tmp))
        conn = open_ro(db)
        total = scalar(conn, f"SELECT COUNT(*) FROM server_files WHERE {IS_TOMBSTONE}")
        folders = scalar(
            conn, f"SELECT COUNT(*) FROM server_files WHERE {IS_TOMBSTONE} AND {IS_FOLDER}"
        )
        prefix = (EXCLUDED_FOLDER, EXCLUDED_FOLDER + "/%")
        under_nm = scalar(
            conn,
            f"SELECT COUNT(*) FROM server_files "
            f"WHERE {IS_TOMBSTONE} AND (path = ? OR path LIKE ?)",
            prefix,
        )
        # EX-6 quotes 24,041 files + 2,943 folders *under the prefix*. Report
        # that split too: the runs/ deletions shift the vault-wide totals, so
        # without this the operator sees neither expected number and may abort
        # a run that is in fact correct.
        under_nm_folders = scalar(
            conn,
            f"SELECT COUNT(*) FROM server_files "
            f"WHERE {IS_TOMBSTONE} AND {IS_FOLDER} AND (path = ? OR path LIKE ?)",
            prefix,
        )
        conn.close()

    files = total - folders
    log()
    log(f"  tombstones, file rows   : {files:>8,}")
    log(f"  tombstones, folder rows : {folders:>8,}")
    log(f"  tombstones, total       : {total:>8,}")
    log()
    log(f"  under {EXCLUDED_FOLDER}:")
    log(f"    file rows   : {under_nm - under_nm_folders:>8,}   (EX-6 expects ~24,041)")
    log(f"    folder rows : {under_nm_folders:>8,}   (EX-6 expects ~2,943)")
    log(f"    total       : {under_nm:>8,}")
    log(f"  elsewhere in the vault    : {total - under_nm:>8,}")

    if total == 0:
        log()
        log("  Zero tombstones. The deletions did not reach the server.")
        log("  Tightening the filters now would strand the remote copies permanently,")
        log("  because exclusion is not retroactive.")
        die("EX-6 hard stop: tombstone count is zero, no filter change made")

    log()
    log("  Tombstones present. The deletions reached the server.")
    log()
    log("  STOP. EX-11 requires Yulong's explicit approval of this split")
    log("  before `filters` may run. Pass it through:")
    log("      vault_sync.py filters --approved-by <name> --approval-note '<what was approved>'")
    return 0


# --------------------------------------------------------------------------
# EX-7: filters
# --------------------------------------------------------------------------


def cmd_filters(args: argparse.Namespace) -> int:
    state_dir = find_state_dir()
    cfg = read_config(state_dir)

    rule("EX-7  filter tightening")
    log("  Config before (non-secret keys only):")
    print_config_safely(cfg)

    if not FILE_TYPES:
        die("refusing to pass an empty --file-types; it falls back to a wider default")

    # EX-6's hard stop belongs to the tool, not to operator discipline. Gating
    # only on --approved-by would let `filters` run without `verify` ever having
    # been executed, tightening the exclusion while the deletions are still only
    # local -- which strands the remote copies permanently, with no undo.
    # Re-read the count here so the EX-6 -> EX-7 ordering is a code invariant.
    with tempfile.TemporaryDirectory(prefix="vault-sync-filters-") as tmp:
        conn = open_ro(copy_state_db(state_dir, Path(tmp)))
        tombstones = scalar(conn, f"SELECT COUNT(*) FROM server_files WHERE {IS_TOMBSTONE}")
        conn.close()
    log()
    log(f"  EX-6 re-check, tombstones in server_files: {tombstones:,}")
    if tombstones == 0:
        log("  Exclusion is not retroactive: tightening now would strand the")
        log("  remote copies with no way to reclaim the quota they hold.")
        die("EX-6 hard stop: zero tombstones, refusing to tighten filters")

    log()
    log(f"  approved by : {args.approved_by}")
    log(f"  note        : {args.approval_note}")

    cmd = [
        "ob",
        "sync-config",
        "--path",
        str(VAULT),
        "--file-types",
        FILE_TYPES,
        "--excluded-folders",
        EXCLUDED_FOLDER,
    ]
    log()
    log(f"  $ {' '.join(cmd)}")
    log("  --configs omitted: omission preserves the existing setting.")

    if args.dry_run:
        log("  Dry run, not executing.")
        return 0

    proc = subprocess.run(cmd, cwd=str(VAULT))
    if proc.returncode != 0:
        die(f"ob sync-config exited {proc.returncode}")

    cfg_after = read_config(state_dir)
    log()
    log("  Config after (non-secret keys only):")
    print_config_safely(cfg_after)

    # Post-condition checks run after the mutation, so clean=False: the config
    # write has already landed and the operator must not be told otherwise.
    excluded = cfg_after.get(EXCLUSION_KEY) or []
    if EXCLUDED_FOLDER not in excluded:
        die(f"exclusion did not take: {EXCLUSION_KEY} is {excluded!r}", clean=False)

    # EC-5 requires both halves verified, not just the exclusion.
    want_types = sorted(t.strip() for t in FILE_TYPES.split(","))
    got_types = sorted(cfg_after.get("allowTypes") or [])
    if got_types != want_types:
        die(f"file types did not take: allowTypes is {got_types!r}, expected {want_types!r}",
            clean=False)

    log()
    log(f"  Exclusion confirmed in config.json ({EXCLUSION_KEY}).")
    log(f"  File types confirmed: {got_types!r}")
    return 0


# --------------------------------------------------------------------------
# EX-9: tripwire
# --------------------------------------------------------------------------


def cmd_tripwire(args: argparse.Namespace) -> int:
    state_dir = find_state_dir()
    cfg = read_config(state_dir)
    excluded = list(cfg.get(EXCLUSION_KEY) or [])

    def is_excluded(rel: str) -> bool:
        # Mirrors the ob matcher exactly: vault-relative, segment-safe, case-sensitive.
        return any(rel == f or rel.startswith(f + "/") for f in excluded)

    max_bytes = int(args.max_file_mb * MB)
    regenerable: list[tuple[str, int]] = []
    oversized: list[tuple[str, int]] = []

    for dirpath, dirnames, filenames in os.walk(VAULT):
        rel_dir = os.path.relpath(dirpath, VAULT)
        rel_dir = "" if rel_dir == "." else rel_dir

        if rel_dir and is_excluded(rel_dir):
            dirnames[:] = []
            continue
        if ".git" in dirnames:
            dirnames.remove(".git")

        for name in list(dirnames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if name in REGENERABLE_DIRS and not is_excluded(rel):
                # These trees churn (a build can unlink an entry mid-walk); a
                # vanished file must not kill the unattended daily run.
                size = 0
                for p in (Path(dirpath) / name).rglob("*"):
                    try:
                        if p.is_file() and not p.is_symlink():
                            size += p.stat().st_size
                    except OSError:
                        continue
                regenerable.append((rel, size))
                dirnames.remove(name)

        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size > max_bytes:
                oversized.append((rel, size))

    rule("EX-9  tripwire")
    log(f"  vault        : {VAULT}")
    log(f"  exclusions   : {excluded or 'none'}")
    log(f"  size ceiling : {args.max_file_mb:.2f} MB")

    if regenerable:
        log()
        log(f"  UNEXCLUDED REGENERABLE DIRECTORIES ({len(regenerable)}):")
        for rel, size in sorted(regenerable, key=lambda x: -x[1]):
            log(f"    {fmt_mb(size):>12}  {rel}")

    if oversized:
        log()
        log(f"  FILES OVER THE {args.max_file_mb:.2f} MB CAP ({len(oversized)}):")
        log("    (these never reach the remote at all -- silent failure, not quota)")
        for rel, size in sorted(oversized, key=lambda x: -x[1]):
            log(f"    {fmt_mb(size):>12}  {rel}")

    if not regenerable and not oversized:
        log()
        log("  Clean. No findings.")
        return 0

    log()
    log(f"  {len(regenerable)} regenerable dir(s), {len(oversized)} oversized file(s).")
    # Non-zero exit is the notification trigger: the cron wrapper notifies on
    # findings only, so a clean run must stay silent.
    return 2


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vault_sync.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gate", help="EX-2 read-only state.db checks")
    g.add_argument("--allow-drift", action="store_true", help="proceed despite drift from the baseline")
    g.set_defaults(func=cmd_gate)

    s = sub.add_parser("stage", help="EX-3/EX-4 compute the deletion set and move it out")
    s.add_argument("--what", choices=("runs", "node-modules", "both"), default="both")
    s.add_argument("--execute", action="store_true", help="actually move (default is dry run)")
    s.add_argument("--holding", help="holding directory outside the vault")
    s.set_defaults(func=cmd_stage)

    y = sub.add_parser("sync", help="EX-5 bidirectional sync")
    y.add_argument("--dry-run", action="store_true")
    y.set_defaults(func=cmd_sync)

    v = sub.add_parser("verify", help="EX-6 tombstone read, split by folder")
    v.set_defaults(func=cmd_verify)

    f = sub.add_parser("filters", help="EX-7 the single sync-config call")
    f.add_argument("--approved-by", required=True, help="who approved the EX-6 -> EX-7 transition")
    f.add_argument("--approval-note", required=True, help="what was approved, recorded in the log")
    f.add_argument("--dry-run", action="store_true")
    f.set_defaults(func=cmd_filters)

    t = sub.add_parser("tripwire", help="EX-9 regenerable dirs and oversized files")
    t.add_argument("--max-file-mb", type=float, default=DEFAULT_MAX_FILE_MB)
    t.set_defaults(func=cmd_tripwire)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
