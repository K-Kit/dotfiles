"""Regression tests for the EX-6 safety gate in scripts/vault/vault_sync.py.

The gate decides whether it is safe to tighten Obsidian's folder exclusion.
Exclusion is *not* retroactive: if it lands while the remote `node_modules`
rows are still live, those ~285 MB strand on the server permanently with no
way to reclaim the quota. So the gate must key on tombstones **under the
folder being excluded**, never on a vault-wide count -- an unrelated deletion
elsewhere in the vault must not unlock it.

This has already gone wrong twice, in the same shape both times:
  QR-6  -- counted `local_files`, where no row ever carries `deleted`
            (structurally always zero).
  P1    -- counted `server_files` vault-wide, so staging the 25 runs/ files
            made the count nonzero while every node_modules row was live.

Run: uv run --no-project python -m pytest tests/test_vault_sync_gate.py
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "scripts" / "vault" / "vault_sync.py"


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


def make_db(vs, *, runs_tombstones: int, nm_tombstones: int, nm_live: int) -> sqlite3.Connection:
    """A server_files table shaped like the real one: (path, data-as-JSON)."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE server_files (path TEXT, data TEXT)")
    nm = vs.EXCLUDED_FOLDER
    for i in range(runs_tombstones):
        db.execute(
            "INSERT INTO server_files VALUES (?,?)",
            (f"research/monitorability/runs/r{i}.jsonl", '{"deleted":true,"folder":false}'),
        )
    for i in range(nm_tombstones):
        db.execute(
            "INSERT INTO server_files VALUES (?,?)",
            (f"{nm}/gone{i}.js", '{"deleted":true,"folder":false}'),
        )
    for i in range(nm_live):
        db.execute(
            "INSERT INTO server_files VALUES (?,?)",
            (f"{nm}/pkg{i}/index.js", '{"deleted":false,"folder":false}'),
        )
    return db


def counts(vs, db) -> tuple[int, int]:
    """(vault-wide, under-the-excluded-folder) -- the two candidate gate inputs."""
    nm = vs.EXCLUDED_FOLDER
    total = db.execute(f"SELECT COUNT(*) FROM server_files WHERE {vs.IS_TOMBSTONE}").fetchone()[0]
    under = db.execute(
        f"SELECT COUNT(*) FROM server_files "
        f"WHERE {vs.IS_TOMBSTONE} AND (path = ? OR path LIKE ?)",
        (nm, nm + "/%"),
    ).fetchone()[0]
    return total, under


def test_runs_deletions_alone_must_not_unlock_the_exclusion(vs):
    """The P1 regression: the exact state after `stage --what runs` + sync."""
    db = make_db(vs, runs_tombstones=25, nm_tombstones=0, nm_live=100)
    total, under = counts(vs, db)
    assert total == 25, "unrelated deletions do reach the vault-wide count"
    assert under == 0, "but no node_modules row has tombstoned"
    # The old gate (`total == 0`) would fall through here and strand ~285 MB.
    assert total != 0 and under == 0


def test_real_node_modules_deletions_do_unlock_it(vs):
    db = make_db(vs, runs_tombstones=25, nm_tombstones=50, nm_live=50)
    _, under = counts(vs, db)
    assert under == 50


def test_empty_server_files_is_a_stop(vs):
    db = make_db(vs, runs_tombstones=0, nm_tombstones=0, nm_live=0)
    assert counts(vs, db) == (0, 0)


def test_live_rows_are_never_counted_as_tombstones(vs):
    """Guards the predicate itself: `deleted:false` must not read as deleted."""
    db = make_db(vs, runs_tombstones=0, nm_tombstones=0, nm_live=500)
    assert counts(vs, db) == (0, 0)


def test_prefix_does_not_leak_to_sibling_paths(vs):
    """`node_modules-old/` must not satisfy a gate about `node_modules/`."""
    db = make_db(vs, runs_tombstones=0, nm_tombstones=0, nm_live=0)
    db.execute(
        "INSERT INTO server_files VALUES (?,?)",
        (vs.EXCLUDED_FOLDER + "-old/x.js", '{"deleted":true,"folder":false}'),
    )
    total, under = counts(vs, db)
    assert total == 1
    assert under == 0, "sibling directory must not satisfy the prefix"


def test_folder_rows_are_distinguishable(vs):
    """EX-6 reports a file/folder split; the predicate must separate them."""
    nm = vs.EXCLUDED_FOLDER
    db = make_db(vs, runs_tombstones=0, nm_tombstones=3, nm_live=0)
    db.execute("INSERT INTO server_files VALUES (?,?)", (nm, '{"deleted":true,"folder":true}'))
    folders = db.execute(
        f"SELECT COUNT(*) FROM server_files WHERE {vs.IS_TOMBSTONE} AND {vs.IS_FOLDER}"
    ).fetchone()[0]
    assert folders == 1
