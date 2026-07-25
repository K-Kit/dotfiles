from __future__ import annotations

import http.client
import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hooks" / "validate_claude_settings.py"

SAMPLE_SCHEMA = {"type": "object"}


def load_module():
    """Import the hook by path (it is a uv PEP-723 script, not an installed module)."""
    spec = importlib.util.spec_from_file_location("validate_claude_settings", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "SCHEMA_CACHE", tmp_path / "cache" / "schema.json")
    return module


def seed_cache(mod, schema=SAMPLE_SCHEMA, *, stale=True):
    mod.SCHEMA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    mod.SCHEMA_CACHE.write_text(json.dumps(schema))
    if stale:
        # Force the fetch path: mtime older than the refresh window.
        import os

        old = 1_000_000
        os.utime(mod.SCHEMA_CACHE, (old, old))


def raise_on_fetch(mod, monkeypatch, exc):
    def boom(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)


# A truncated response raises IncompleteRead, which is an HTTPException and a
# ValueError but NOT an OSError. It used to escape the fallback handler and
# crash the pre-commit hook, blocking commits on any flaky/sandboxed network.
FETCH_FAILURES = [
    http.client.IncompleteRead(b"partial"),
    http.client.BadStatusLine("garbage"),
    urllib.error.URLError("offline"),
    TimeoutError("timed out"),
    OSError("socket blew up"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
]


@pytest.mark.parametrize("exc", FETCH_FAILURES, ids=lambda e: type(e).__name__)
def test_fetch_failure_falls_back_to_stale_cache(mod, monkeypatch, exc):
    seed_cache(mod)
    raise_on_fetch(mod, monkeypatch, exc)
    assert mod._load_schema() == SAMPLE_SCHEMA


@pytest.mark.parametrize("exc", FETCH_FAILURES, ids=lambda e: type(e).__name__)
def test_fetch_failure_without_cache_returns_none(mod, monkeypatch, exc):
    raise_on_fetch(mod, monkeypatch, exc)
    assert mod._load_schema() is None


def test_corrupt_cache_is_refetched(mod, monkeypatch):
    mod.SCHEMA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    mod.SCHEMA_CACHE.write_text("{ not json")
    raise_on_fetch(mod, monkeypatch, urllib.error.URLError("offline"))
    assert mod._load_schema() is None


def test_unwritable_cache_still_returns_fetched_schema(mod, monkeypatch):
    """A cache-write failure must not discard a schema we successfully fetched."""

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(SAMPLE_SCHEMA).encode()

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: FakeResponse())

    def no_write(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", no_write)
    assert mod._load_schema() == SAMPLE_SCHEMA


def test_valid_settings_passes_without_any_schema(mod, monkeypatch, tmp_path):
    """End-to-end: a good settings file validates even when the schema is unavailable."""
    raise_on_fetch(mod, monkeypatch, http.client.IncompleteRead(b"partial"))
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "statusLine": {"type": "command", "command": "x"},
                "hooks": {"A": [], "B": [], "C": []},
                "permissions": {"allow": [f"Bash(cmd{i})" for i in range(25)]},
                **{f"k{i}": i for i in range(12)},
            }
        )
    )
    errors, _warnings = mod.validate(settings)
    assert errors == []
