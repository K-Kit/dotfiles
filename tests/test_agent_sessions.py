"""Tests for custom_bins/agent-sessions (fleet view + wait/watch/in-use).

Subprocess tests drive the real executable against a synthetic
CLAUDE_CONFIG_DIR (which covers both the claude registry and the
codex-companion job state, since the latter lives under plugins/data/).
Liveness fixtures use this test process's own pid (alive in any PID
namespace, sandboxed or not) and a spawned-then-exited child (dead).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "custom_bins" / "agent-sessions"


def load_module():
    loader = importlib.machinery.SourceFileLoader("agent_sessions", str(TOOL))
    spec = importlib.util.spec_from_loader("agent_sessions", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


MOD = load_module()


def run_tool(args, cfg):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg))
    # Pin to the two file-backed sources (both synthetic under cfg) unless the
    # test picks its own: in a host PID namespace the codex-proc /proc scan
    # would otherwise pick up whatever codex the developer happens to be running.
    if "--source" not in args:
        args = [*args, "--source", "claude,codex-job"]
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def write_claude(cfg, pid, status="idle", name="sess", session_id="aaaa1111-0000",
                 procstart=None, cwd="/tmp"):
    registry = cfg / "sessions"
    registry.mkdir(parents=True, exist_ok=True)
    record = {
        "sessionId": session_id, "cwd": cwd, "pid": pid, "status": status,
        "statusUpdatedAt": int(time.time() * 1000), "name": name,
        "kind": "interactive",
    }
    if procstart is not None:
        record["procStart"] = procstart
    path = registry / f"{pid}.json"
    path.write_text(json.dumps(record))
    return path


def write_job(cfg, job_id="task-abc123", status="running", pid=None, root="/tmp/wsp"):
    jobs = cfg / "plugins/data/codex-codex-plugin-cc/state/wsp-123/jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    record = {
        "id": job_id, "kind": "task", "status": status, "pid": pid,
        "workspaceRoot": root, "createdAt": "2026-08-16T00:00:00.000Z",
    }
    path = jobs / f"{job_id}.json"
    path.write_text(json.dumps(record))
    return path


# ---------- list ----------

def test_list_json_live_and_stale(tmp_path):
    write_claude(tmp_path, os.getpid(), status="busy", name="live-one")
    write_claude(tmp_path, dead_pid(), status="idle", name="crashed")

    result = run_tool(["--json"], tmp_path)
    assert result.returncode == 0
    records = json.loads(result.stdout)
    assert [r["name"] for r in records] == ["live-one"]
    assert records[0]["source"] == "claude"
    assert records[0]["status"] == "busy"
    assert records[0]["live"] is True

    result = run_tool(["--json", "--all"], tmp_path)
    records = json.loads(result.stdout)
    assert sorted(r["name"] for r in records) == ["crashed", "live-one"]
    stale = next(r for r in records if r["name"] == "crashed")
    assert stale["live"] is False and stale["stale"] is True


def test_list_json_empty_when_no_registry(tmp_path):
    result = run_tool(["--json"], tmp_path / "nonexistent")
    assert result.returncode == 0
    assert json.loads(result.stdout) == []


def test_procstart_mismatch_marks_stale(tmp_path):
    # Own pid is alive, but a procStart token from a different process
    # instance must read as a recycled pid, i.e. stale.
    write_claude(tmp_path, os.getpid(), name="recycled", procstart="1")
    records = json.loads(run_tool(["--json", "--all"], tmp_path).stdout)
    assert records[0]["live"] is False


def test_default_subcommand_is_list(tmp_path):
    write_claude(tmp_path, os.getpid(), name="tabular")
    result = run_tool([], tmp_path)
    assert result.returncode == 0
    assert result.stdout.splitlines()[0].split() == [
        "NAME", "STATUS", "AGE", "PID", "SOURCE", "KIND", "CWD",
    ]
    assert "tabular" in result.stdout


# ---------- codex jobs ----------

def test_codex_job_states(tmp_path):
    write_job(tmp_path, job_id="run-live", status="running", pid=os.getpid())
    write_job(tmp_path, job_id="run-stale", status="running", pid=dead_pid())
    write_job(tmp_path, job_id="old-done", status="completed", pid=None)

    live = json.loads(run_tool(["--json"], tmp_path).stdout)
    assert [r["name"] for r in live] == ["run-live"]
    assert live[0]["source"] == "codex-job"

    everything = {r["name"]: r for r in json.loads(run_tool(["--json", "--all"], tmp_path).stdout)}
    assert everything["run-stale"]["stale"] is True
    assert everything["old-done"]["status"] == "completed"
    assert everything["old-done"]["stale"] is False


def test_source_filter(tmp_path):
    write_claude(tmp_path, os.getpid(), name="claude-one")
    write_job(tmp_path, job_id="job-one", status="running", pid=os.getpid())
    records = json.loads(run_tool(["--json", "--source", "codex-job"], tmp_path).stdout)
    assert [r["name"] for r in records] == ["job-one"]


# ---------- wait ----------

def test_wait_reaches_state(tmp_path):
    write_claude(tmp_path, os.getpid(), status="busy", name="worker")

    def flip():
        time.sleep(0.3)
        write_claude(tmp_path, os.getpid(), status="idle", name="worker")

    thread = threading.Thread(target=flip)
    thread.start()
    result = run_tool(["wait", "worker", "--until", "idle", "--interval", "0.1", "--timeout", "10"], tmp_path)
    thread.join()
    assert result.returncode == 0
    assert "worker idle" in result.stdout


def test_wait_timeout_is_exit_3(tmp_path):
    write_claude(tmp_path, os.getpid(), status="busy", name="stuck")
    result = run_tool(["wait", "stuck", "--until", "waiting", "--interval", "0.1", "--timeout", "0.4"], tmp_path)
    assert result.returncode == 3


def test_wait_missing_target_is_exit_4(tmp_path):
    write_claude(tmp_path, os.getpid(), name="present")
    result = run_tool(["wait", "no-such", "--until", "idle", "--timeout", "5"], tmp_path)
    assert result.returncode == 4


def test_wait_until_gone_on_missing_target_succeeds(tmp_path):
    (tmp_path / "sessions").mkdir(parents=True)
    result = run_tool(["wait", "no-such", "--until", "gone", "--timeout", "5"], tmp_path)
    assert result.returncode == 0


def test_wait_until_done_on_completed_job(tmp_path):
    write_job(tmp_path, job_id="review-x1", status="completed")
    result = run_tool(["wait", "review-x1", "--until", "done", "--timeout", "5"], tmp_path)
    assert result.returncode == 0


def test_wait_exits_4_when_target_dies_mid_wait(tmp_path):
    path = write_claude(tmp_path, os.getpid(), status="busy", name="doomed")

    def vanish():
        time.sleep(0.3)
        path.unlink()

    thread = threading.Thread(target=vanish)
    thread.start()
    result = run_tool(["wait", "doomed", "--until", "waiting", "--interval", "0.1", "--timeout", "10"], tmp_path)
    thread.join()
    assert result.returncode == 4


def test_wait_ambiguous_prefix_is_exit_2(tmp_path):
    write_claude(tmp_path, os.getpid(), name="feat-a", session_id="s1")
    write_claude(tmp_path, dead_pid(), name="feat-b", session_id="s2")
    result = run_tool(["wait", "feat", "--until", "idle", "--timeout", "5"], tmp_path)
    assert result.returncode == 2


# ---------- in-use ----------

def test_in_use_containment(tmp_path):
    wt = tmp_path / "trees" / "wt-a"
    wt.mkdir(parents=True)
    write_claude(tmp_path, os.getpid(), name="inside", cwd=str(wt / "sub"))

    hit = run_tool(["in-use", str(wt)], tmp_path)
    assert hit.returncode == 0
    assert "inside" in hit.stdout

    miss = run_tool(["in-use", str(tmp_path / "trees" / "wt-b")], tmp_path)
    assert miss.returncode == 1
    assert miss.stdout == ""


def test_in_use_ignores_dead_sessions(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    write_claude(tmp_path, dead_pid(), name="ghost", cwd=str(wt))
    assert run_tool(["in-use", str(wt)], tmp_path).returncode == 1


# ---------- watch ----------

def test_watch_emits_transition(tmp_path):
    write_claude(tmp_path, os.getpid(), status="busy", name="watched")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(tmp_path))
    proc = subprocess.Popen(
        [sys.executable, str(TOOL), "watch", "--interval", "0.05", "--json",
         "--source", "claude,codex-job"],
        stdout=subprocess.PIPE, text=True, env=env,
    )
    try:
        time.sleep(0.3)  # let the baseline snapshot land
        write_claude(tmp_path, os.getpid(), status="idle", name="watched")
        line = {}

        def read_line():
            line["value"] = proc.stdout.readline()

        reader = threading.Thread(target=read_line)
        reader.start()
        reader.join(timeout=10)
        event = json.loads(line["value"])
        assert event["name"] == "watched"
        assert (event["old"], event["new"]) == ("busy", "idle")
    finally:
        proc.kill()
        proc.wait()


# ---------- unit-level ----------

def test_parse_iso_ms():
    expected = int(datetime(2026, 8, 16, 10, 36, 33, 322000, tzinfo=timezone.utc).timestamp() * 1000)
    assert MOD.parse_iso_ms("2026-08-16T10:36:33.322Z") == expected
    assert MOD.parse_iso_ms(None) is None
    assert MOD.parse_iso_ms("not-a-date") is None


def test_codex_infra_procs_filtered_agents_kept():
    # The scanner's skip predicate is codex_subcommand(...) in CODEX_INFRA_SUBCOMMANDS.
    # codex-companion's post-job broker must be filtered; TUI and exec runs kept.
    assert MOD.codex_subcommand(["/usr/local/bin/codex", "app-server"]) == "app-server"
    assert "app-server" in MOD.CODEX_INFRA_SUBCOMMANDS
    assert MOD.codex_subcommand(["codex"]) is None
    assert None not in MOD.CODEX_INFRA_SUBCOMMANDS
    assert MOD.codex_subcommand(["codex", "--full-auto", "exec", "fix the bug"]) == "exec"
    assert "exec" not in MOD.CODEX_INFRA_SUBCOMMANDS
    # /proc/<pid>/cmdline is NUL-terminated, so the split leaves a trailing ""
    assert MOD.codex_subcommand(["codex", "app-server", ""]) == "app-server"
    # `-c key=val` config overrides: the value is not a subcommand (live TUI shape)
    assert MOD.codex_subcommand(["codex", "-c", "features.code_mode_host=true"]) is None
    # Real broker argv observed live: the k=v prefix must not mask the subcommand
    broker = ["codex", "-c", "features.code_mode_host=true", "app-server", "--listen", "unix://"]
    assert MOD.codex_subcommand(broker) == "app-server"


def test_pid_ancestors_contains_self_and_parent():
    ancestors = MOD.pid_ancestors(os.getpid())
    assert os.getpid() in ancestors
    assert os.getppid() in ancestors


def test_assign_to_worktrees_longest_prefix_wins():
    worktrees = [
        {"path": "/repo", "branch": "main"},
        {"path": "/repo/.claude/worktrees/x", "branch": "worktree-x"},
    ]
    nested = {"cwd": "/repo/.claude/worktrees/x/sub"}
    toplevel = {"cwd": "/repo/other"}
    outside = {"cwd": "/elsewhere"}
    assigned = MOD.assign_to_worktrees(worktrees, [nested, toplevel, outside])
    assert assigned["/repo/.claude/worktrees/x"] == [nested]
    assert assigned["/repo"] == [toplevel]


def test_resolve_exact_beats_prefix():
    records = [
        {"name": "wt", "pid": 111, "session_id": None},
        {"name": "wt2", "pid": 222, "session_id": None},
    ]
    assert [r["name"] for r in MOD.resolve(records, "wt")] == ["wt"]
    assert [r["name"] for r in MOD.resolve(records, "w")] == ["wt", "wt2"]
    assert [r["name"] for r in MOD.resolve(records, "222")] == ["wt2"]


def test_parse_worktree_porcelain():
    text = (
        "worktree /repo\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /repo/.claude/worktrees/x\nHEAD def\ndetached\n"
    )
    parsed = MOD.parse_worktree_porcelain(text)
    assert parsed == [
        {"path": "/repo", "branch": "main"},
        {"path": "/repo/.claude/worktrees/x", "branch": "(detached)"},
    ]
