"""Regression tests for the EX-6/EX-7 safety gate in scripts/vault/vault_sync.py.

The gate decides whether it is safe to tighten Obsidian's folder exclusion.
Exclusion is *not* retroactive: if it lands while remote `node_modules` rows are
still live, those ~285 MB strand on the server permanently with no way to
reclaim the quota. The invariant is therefore **zero live rows under the exact
folder being excluded** -- not "some tombstones exist somewhere".

That invariant has now been got wrong three times, each time in the same shape:

  QR-6  counted `local_files`, where no row ever carries `deleted`
        (structurally always zero).
  P1    counted `server_files` vault-wide, so staging the 25 runs/ files made
        the count nonzero while every node_modules row was still live.
  P2    counted tombstones under the right prefix but unlocked on `>= 1`, so a
        sync that died after one row unlocked the exclusion for the other
        26,983.

The shared lesson is that a gate is only as good as the thing the tests
actually call. The previous version of this file re-implemented the gate's SQL
in a local `counts()` helper, so reverting any production gate left every test
green -- it tested a copy of the logic, not the logic. Every test below drives
the shipped `cmd_verify` / `cmd_filters` / `cmd_tripwire` entry points end to
end, from SYNC_ROOT discovery through the real state.db copy (WAL sidecars
included) to the real `die()`.

Run: uv run --no-project --with pytest python -m pytest tests/test_vault_sync_gate.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "scripts" / "vault" / "vault_sync.py"

TOMBSTONE = '{"deleted":true,"folder":false}'
TOMBSTONE_DIR = '{"deleted":true,"folder":true}'
LIVE = '{"deleted":false,"folder":false}'


def load_tool():
    spec = importlib.util.spec_from_file_location("vault_sync_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves its module via sys.modules.
    sys.modules["vault_sync_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def vs():
    return load_tool()


# --------------------------------------------------------------------------
# Fixtures that build a real on-disk sync state directory
# --------------------------------------------------------------------------


def rows_for(vs, *, tombstones=0, live=0, tombstone_dirs=0, elsewhere=0, under=None):
    """server_files rows shaped like the real table: (path, data-as-JSON)."""
    folder = under if under is not None else vs.EXCLUDED_FOLDER
    out = []
    for i in range(tombstones):
        out.append((f"{folder}/gone{i}.js", TOMBSTONE))
    for i in range(tombstone_dirs):
        out.append((f"{folder}/d{i}", TOMBSTONE_DIR))
    for i in range(live):
        out.append((f"{folder}/pkg{i}/index.js", LIVE))
    for i in range(elsewhere):
        out.append((f"research/monitorability/runs/r{i}.jsonl", TOMBSTONE))
    return out


def make_sync_root(vs, monkeypatch, tmp_path, rows, *, wal=False, local_rows=(), excluded=()):
    """A SYNC_ROOT the shipped find_state_dir()/copy_state_db() can walk for real.

    `wal=True` leaves the inserts uncheckpointed in state.db-wal by holding the
    writer open, reproducing the state a SIGTERM'd sync leaves behind. Reading
    state.db alone would then see none of them.
    """
    root = tmp_path / "sync"
    state = root / "vault-abc123"
    state.mkdir(parents=True)
    (state / "config.json").write_text(
        json.dumps({vs.EXCLUSION_KEY: list(excluded),
                    "allowTypes": ["image", "pdf"], "encryptionKey": "x"})
    )

    db = sqlite3.connect(state / "state.db")
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE server_files (path TEXT, data TEXT)")
    db.execute("CREATE TABLE local_files (path TEXT, data TEXT)")
    db.executemany("INSERT INTO server_files VALUES (?,?)", rows)
    db.executemany("INSERT INTO local_files VALUES (?,?)", local_rows)
    db.commit()
    if wal:
        assert (state / "state.db-wal").exists(), "test setup: expected an uncheckpointed WAL"
        # Deliberately left open: closing the last connection checkpoints.
    else:
        db.close()

    monkeypatch.setattr(vs, "SYNC_ROOT", root)
    return state


def verify(vs) -> int:
    return vs.cmd_verify(argparse.Namespace())


def filters(vs, **over) -> int:
    args = argparse.Namespace(approved_by="tester", approval_note="test", dry_run=True)
    for k, v in over.items():
        setattr(args, k, v)
    return vs.cmd_filters(args)


# --------------------------------------------------------------------------
# The three historical defects. Each must stop the gate.
# --------------------------------------------------------------------------


def test_p2_one_tombstone_does_not_unlock_while_rows_are_live(vs, monkeypatch, tmp_path):
    """The Critical: a sync that died after one row must not unlock the exclusion.

    This is the state a SIGTERM'd or rate-limited sync leaves behind, and it is
    the most likely state to meet in practice -- which is exactly why a
    `tombstones >= 1` gate is dangerous rather than merely imprecise.
    """
    make_sync_root(vs, monkeypatch, tmp_path, rows_for(vs, tombstones=1, live=100))
    with pytest.raises(SystemExit):
        verify(vs)
    with pytest.raises(SystemExit):
        filters(vs)


def test_p1_unrelated_deletions_do_not_unlock(vs, monkeypatch, tmp_path):
    """The exact state after `stage --what runs` + sync: 25 tombstones, none here."""
    make_sync_root(vs, monkeypatch, tmp_path, rows_for(vs, elsewhere=25, live=100))
    with pytest.raises(SystemExit):
        verify(vs)
    with pytest.raises(SystemExit):
        filters(vs)


def test_qr6_local_files_tombstones_do_not_unlock(vs, monkeypatch, tmp_path):
    """Deletions recorded locally but never pushed are not evidence of anything."""
    make_sync_root(
        vs,
        monkeypatch,
        tmp_path,
        rows_for(vs, live=100),
        local_rows=[(f"{vs.EXCLUDED_FOLDER}/gone{i}.js", TOMBSTONE) for i in range(500)],
    )
    with pytest.raises(SystemExit):
        verify(vs)


# --------------------------------------------------------------------------
# The prefix must mean this folder and nothing else
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sibling",
    [
        # `_` is a single-character LIKE wildcard, so a LIKE-based prefix test
        # matches all of these. substr() comparison does not.
        "research/monitorability/slides/slidev/node-modules",
        "research/monitorability/slides/slidev/nodeXmodules",
        # LIKE is ASCII case-insensitive by default; the real matcher is not.
        "research/monitorability/slides/slidev/NODE_MODULES",
        # Plain suffix collision.
        "research/monitorability/slides/slidev/node_modules-old",
    ],
)
def test_sibling_directories_cannot_unlock_the_gate(vs, monkeypatch, tmp_path, sibling):
    assert sibling != vs.EXCLUDED_FOLDER
    make_sync_root(vs, monkeypatch, tmp_path, rows_for(vs, tombstones=500, under=sibling))
    with pytest.raises(SystemExit):
        verify(vs)


def test_sibling_live_rows_do_not_block_a_clean_target(vs, monkeypatch, tmp_path):
    """The prefix must be exact in both directions, not merely conservative."""
    rows = rows_for(vs, tombstones=20) + rows_for(
        vs, live=50, under="research/monitorability/slides/slidev/node-modules"
    )
    make_sync_root(vs, monkeypatch, tmp_path, rows)
    assert verify(vs) == 0


# --------------------------------------------------------------------------
# The tombstone predicate itself
# --------------------------------------------------------------------------


# Every case here pairs the ambiguous rows with REAL tombstones. Without that
# pairing the folder has zero tombstones, so `exclusion_blocker` stops on its
# "no rows at all" branch and the test passes no matter what the predicate does
# to the ambiguous rows -- which is exactly how the three-valued-logic defect
# below survived a green suite. With a real tombstone present, the live count is
# the only thing that can block, so these tests bind the predicate itself.
AMBIGUOUS = [
    pytest.param('{"deleted":%s,"folder":false}' % v, id=f"deleted={v}")
    for v in ['"0"', '"no"', "{}", "[]", '"false"', "0", "null", "1", '"true"']
] + [
    pytest.param('{"folder":false}', id="key-absent"),
    pytest.param("not valid json at all", id="malformed-json"),
    pytest.param("", id="empty-string"),
    pytest.param(None, id="NULL-blob"),
]


@pytest.mark.parametrize("data", AMBIGUOUS)
def test_ambiguous_rows_block_even_beside_real_tombstones(vs, monkeypatch, tmp_path, data):
    """Anything not unambiguously JSON `true` counts as LIVE, so ambiguity blocks.

    Covers three distinct ways a row used to escape classification:
      * broad truthiness -- `"deleted":"0"` read as deleted;
      * SQL three-valued logic -- an absent key made both IS_TOMBSTONE and
        NOT(IS_TOMBSTONE) evaluate to NULL, so the row matched NEITHER count and
        vanished, leaving `tombstones > 0, live == 0` and an unlocked gate;
      * `{"deleted":1}` / `{"deleted":"true"}` -- indistinguishable from a real
        `true` under json_extract, so classification now uses json_type.
    """
    rows = [(f"{vs.EXCLUDED_FOLDER}/real{i}.js", TOMBSTONE) for i in range(30)]
    rows += [(f"{vs.EXCLUDED_FOLDER}/amb{i}.js", data) for i in range(5)]
    make_sync_root(vs, monkeypatch, tmp_path, rows)
    with pytest.raises(SystemExit):
        verify(vs)


def test_row_accounting_covers_every_row_under_the_target(vs, monkeypatch, tmp_path):
    """tombstones + live must equal the rows examined -- no row falls through.

    The structural backstop. Each gate defect so far was some row not being
    counted where it belonged; the counts still looked plausible, so nothing
    caught it. Asserting the classifier is total catches that class directly.
    """
    rows = [(f"{vs.EXCLUDED_FOLDER}/t{i}.js", TOMBSTONE) for i in range(10)]
    rows += [(f"{vs.EXCLUDED_FOLDER}/l{i}.js", LIVE) for i in range(4)]
    rows += [(f"{vs.EXCLUDED_FOLDER}/weird{i}.js", d)
             for i, d in enumerate(['{"folder":false}', "bad json", None, '{"deleted":1}'])]
    state = make_sync_root(vs, monkeypatch, tmp_path, rows)

    conn = vs.open_ro(state / "state.db")
    counted = vs.count_target_rows(conn)
    examined = vs.scalar(
        conn,
        "SELECT COUNT(*) FROM server_files WHERE %s" % vs.under_target(vs.EXCLUDED_FOLDER)[0],
        vs.under_target(vs.EXCLUDED_FOLDER)[1],
    )
    conn.close()

    assert examined == len(rows)
    assert counted.tombstones + counted.live == examined
    assert counted.tombstones == 10          # only the literal `true` rows
    assert counted.live == len(rows) - 10    # everything else, ambiguity included


def test_null_path_rows_stop_the_tool(vs, monkeypatch, tmp_path):
    """A NULL path matches no prefix test, so it can never be attributed."""
    rows = [(f"{vs.EXCLUDED_FOLDER}/t{i}.js", TOMBSTONE) for i in range(30)]
    rows += [(None, LIVE)]
    make_sync_root(vs, monkeypatch, tmp_path, rows)
    with pytest.raises(SystemExit):
        verify(vs)


# --------------------------------------------------------------------------
# The states that legitimately pass, and the empty one that does not
# --------------------------------------------------------------------------


def test_existing_exclusions_are_re_listed_not_dropped(vs, monkeypatch, tmp_path, capsys):
    """--excluded-folders assigns the whole list, so omitting one un-excludes it.

    `ob sync-config --help`: "Folders to exclude, comma-separated (empty string
    to clear)". Passing only our own folder would silently un-exclude everything
    else already there, and a post-condition that merely asked "is our folder
    present?" would still report success.
    """
    make_sync_root(
        vs, monkeypatch, tmp_path,
        rows_for(vs, tombstones=24041, tombstone_dirs=2943),
        excluded=["some/other/folder", "a/third/one"],
    )
    assert filters(vs) == 0
    printed = capsys.readouterr().out
    sent = [ln for ln in printed.splitlines() if "--excluded-folders" in ln]
    assert sent, "expected the composed ob command to be logged"
    assert "some/other/folder" in sent[0]
    assert "a/third/one" in sent[0]
    assert vs.EXCLUDED_FOLDER in sent[0]


def test_fully_synced_deletions_unlock_the_gate(vs, monkeypatch, tmp_path):
    make_sync_root(vs, monkeypatch, tmp_path, rows_for(vs, tombstones=24041, tombstone_dirs=2943))
    assert verify(vs) == 0
    assert filters(vs) == 0


def test_empty_target_folder_is_a_stop(vs, monkeypatch, tmp_path):
    """No rows at all means the deletions never landed, or this is the wrong DB."""
    make_sync_root(vs, monkeypatch, tmp_path, rows_for(vs, elsewhere=25))
    with pytest.raises(SystemExit):
        verify(vs)


def test_wal_resident_tombstones_are_counted(vs, monkeypatch, tmp_path):
    """A SIGTERM'd sync never checkpoints; reading state.db alone would miss it."""
    make_sync_root(vs, monkeypatch, tmp_path, rows_for(vs, tombstones=200), wal=True)
    assert verify(vs) == 0


def test_wal_resident_live_rows_still_block(vs, monkeypatch, tmp_path):
    make_sync_root(vs, monkeypatch, tmp_path, rows_for(vs, tombstones=200, live=5), wal=True)
    with pytest.raises(SystemExit):
        verify(vs)


# --------------------------------------------------------------------------
# filters must not reach the mutation when the gate says no
# --------------------------------------------------------------------------


def test_filters_never_invokes_ob_when_blocked(vs, monkeypatch, tmp_path):
    """Proves the stop precedes the irreversible call, not merely accompanies it."""
    make_sync_root(vs, monkeypatch, tmp_path, rows_for(vs, tombstones=1, live=100))

    def explode() -> str:
        raise AssertionError("resolve_ob() reached despite live rows under the target")

    monkeypatch.setattr(vs, "resolve_ob", explode)
    with pytest.raises(SystemExit):
        filters(vs)


def test_filters_refuses_empty_file_types(vs, monkeypatch, tmp_path):
    """EX-7: an empty --file-types silently falls back to a wider default."""
    make_sync_root(vs, monkeypatch, tmp_path, rows_for(vs, tombstones=100))
    monkeypatch.setattr(vs, "FILE_TYPES", "")
    with pytest.raises(SystemExit):
        filters(vs)


def test_config_printing_withholds_secrets(vs, capsys):
    """EX-10: encryption material must never reach a terminal or a log."""
    vs.print_config_safely({"encryptionKey": "SECRET-K", "encryptionSalt": "SECRET-S",
                            vs.EXCLUSION_KEY: ["a"]})
    out = capsys.readouterr().out
    assert "SECRET-K" not in out and "SECRET-S" not in out
    assert "withheld" in out


# --------------------------------------------------------------------------
# EX-9 tripwire: a scan that looked at nothing must not report clean
# --------------------------------------------------------------------------


def test_tripwire_refuses_to_report_clean_on_a_missing_vault(vs, monkeypatch, tmp_path):
    make_sync_root(vs, monkeypatch, tmp_path, [])
    monkeypatch.setattr(vs, "VAULT", tmp_path / "does-not-exist")
    with pytest.raises(SystemExit):
        vs.cmd_tripwire(argparse.Namespace(max_file_mb=vs.DEFAULT_MAX_FILE_MB))


@pytest.mark.skipif(hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() == 0,
                    reason="root ignores directory permissions, so nothing is unreadable")
def test_tripwire_refuses_to_report_clean_when_a_subtree_is_unreadable(vs, monkeypatch, tmp_path):
    """A partial scan is not a clean scan.

    os.walk swallows per-directory errors by default, so an unreadable subtree
    silently contributes nothing and the run reports "Clean. No findings." --
    indistinguishable from a vault that really is clean.
    """
    import os as _os

    make_sync_root(vs, monkeypatch, tmp_path, [])
    vault = tmp_path / "vault"
    locked = vault / "locked"
    locked.mkdir(parents=True)
    (locked / "big.bin").write_bytes(b"x")
    _os.chmod(locked, 0o000)
    monkeypatch.setattr(vs, "VAULT", vault)
    try:
        with pytest.raises(SystemExit):
            vs.cmd_tripwire(argparse.Namespace(max_file_mb=vs.DEFAULT_MAX_FILE_MB))
    finally:
        _os.chmod(locked, 0o700)


def test_tripwire_reports_clean_on_a_genuinely_clean_vault(vs, monkeypatch, tmp_path):
    make_sync_root(vs, monkeypatch, tmp_path, [])
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "a.md").write_text("hello")
    monkeypatch.setattr(vs, "VAULT", vault)
    assert vs.cmd_tripwire(argparse.Namespace(max_file_mb=vs.DEFAULT_MAX_FILE_MB)) == 0
