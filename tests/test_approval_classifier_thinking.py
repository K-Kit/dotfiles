#!/usr/bin/env python3
"""Regression tests: BOTH classifier backends must suppress adaptive thinking.

Why this file exists. Sonnet 5 thinks by default when nothing says otherwise,
whereas the Haiku/Sonnet-4.6 generation did not. That difference broke this hook
in two distinct ways, and each backend needed a different remedy:

  * API backend      -- `max_tokens` caps thinking AND response text together, so
                        an adaptive reply spends the budget reasoning and gets
                        truncated before it emits the JSON verdict. Remedy:
                        `thinking: {"type": "disabled"}` in the request body.
  * Subscription CLI -- no max_tokens exists here, so nothing truncates; instead
                        the child thinks while the hook's deadline runs out.
                        Claude Code 2.1.223 has no `--thinking` flag, so the
                        remedy is `--effort low`.

Both failures are silent: the hook fails open to a manual permission prompt, so
a regression looks like "the classifier just stopped helping" rather than an
error. Hence pinning the wire format and the argv rather than trusting comments.

No network and no subprocess: urlopen and subprocess.run are both replaced with
capturing fakes, so this is safe to run anywhere.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Overridable so the suite can be pointed at a deliberately-broken copy to prove
# these tests can actually fail -- a green assertion nobody has seen go red is
# not evidence of anything. See tmp/mutation_check.sh.
HOOK = pathlib.Path(
    os.environ.get("APPROVAL_CLASSIFIER_PATH")
    or ROOT / "claude" / "hooks" / "approval_classifier.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("approval_classifier", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ac():
    return load_module()


# --- constants -------------------------------------------------------------

def test_api_model_is_sonnet_5(ac):
    assert ac.MODEL == "claude-sonnet-5"


def test_subscription_model_is_sonnet(ac):
    assert ac.SUBSCRIPTION_MODEL == "sonnet"


def test_thinking_is_disabled_not_merely_absent(ac):
    """Absence is the bug, so an omitted/None THINKING must fail this test."""
    assert ac.THINKING == {"type": "disabled"}


def test_subscription_effort_is_an_accepted_cli_value(ac):
    # `claude --help` (2.1.223): low, medium, high, xhigh, max. An unrecognised
    # value is NOT an error -- the CLI warns and silently uses the default
    # effort, which would restore the very latency this setting removes.
    assert ac.SUBSCRIPTION_EFFORT in {"low", "medium", "high", "xhigh", "max"}


# --- API backend: the request body actually sent ---------------------------

def test_api_request_body_disables_thinking(ac, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        payload = {
            "content": [{"type": "text",
                         "text": '{"decision":"allow","reason":"test"}'}],
            "stop_reason": "end_turn",
        }

        class Resp:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Resp()

    monkeypatch.setattr(ac.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")

    ac.classify("Bash", {"command": "curl https://example.com"}, "/tmp", "RULES")

    body = captured["body"]
    assert body["thinking"] == {"type": "disabled"}, (
        "Sonnet 5 thinks adaptively when `thinking` is omitted; max_tokens then "
        "covers thinking + text and the JSON verdict gets truncated away."
    )
    assert body["model"] == "claude-sonnet-5"
    # Guard the interaction, not just the field: disabled thinking is rejected
    # at effort xhigh/max, so a future edit adding a high effort would 400.
    assert "effort" not in body or body["effort"] in {"low", "medium", "high"}


# --- Subscription backend: the argv actually spawned ------------------------

def test_subscription_argv_passes_effort(ac, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({
                "is_error": False,
                "result": '{"decision":"allow","reason":"test"}',
            }),
            stderr="",
        )

    monkeypatch.setattr(ac.subprocess, "run", fake_run)

    ac.classify_via_subscription(
        "Bash", {"command": "curl https://example.com"}, "/tmp", "RULES",
    )

    cmd = captured["cmd"]
    assert "--effort" in cmd, (
        "Without --effort the CLI child runs Sonnet 5's adaptive thinking and "
        "can outlive the hook's remaining budget."
    )
    assert cmd[cmd.index("--effort") + 1] == ac.SUBSCRIPTION_EFFORT
    assert cmd[cmd.index("--model") + 1] == ac.SUBSCRIPTION_MODEL
    # --bare must never appear: its help states OAuth and keychain are never
    # read, so it is the one mode that cannot reach the subscription at all.
    assert "--bare" not in cmd


def test_subscription_argv_keeps_its_hardening_flags(ac, monkeypatch):
    """The effort flag must not have displaced any sandboxing flag."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({
                "is_error": False,
                "result": '{"decision":"allow","reason":"test"}',
            }),
            stderr="",
        )

    monkeypatch.setattr(ac.subprocess, "run", fake_run)
    ac.classify_via_subscription("Bash", {"command": "ls"}, "/tmp", "RULES")

    for flag in ("--safe-mode", "--disable-slash-commands",
                 "--strict-mcp-config", "--tools"):
        assert flag in captured["cmd"], f"{flag} was dropped from the child argv"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
