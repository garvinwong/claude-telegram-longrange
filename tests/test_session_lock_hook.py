#!/usr/bin/env python3
# test_session_lock_hook.py — PC 侧心跳 hook 脚本 stdin 灌测
#
# 用 subprocess 驱动 hooks/session_lock_hook.sh，喂 Claude Code hook JSON，
# 用 TGLR_LOCK_DIR 把锁目录指到 tmp。

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import session_lock  # noqa: E402

_HOOK = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "hooks", "session_lock_hook.sh"))


def _run_hook(payload, lockdir, extra_env=None):
    env = dict(os.environ)
    env["TGLR_LOCK_DIR"] = lockdir
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", _HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=20)


@pytest.fixture
def lockdir(tmp_path, monkeypatch):
    d = str(tmp_path / "locks")
    monkeypatch.setattr(session_lock, "LOCK_DIR", d)
    return d


def test_hook_exists():
    assert os.path.exists(_HOOK)


def test_sessionstart_claims_heartbeat(lockdir):
    p = _run_hook({"session_id": "hook-sid", "hook_event_name": "SessionStart"}, lockdir)
    assert p.returncode == 0
    assert p.stdout == "", "hook 绝不能向 stdout 输出"
    assert session_lock.pc_active("hook-sid") is True


def test_pretooluse_refreshes_heartbeat(lockdir):
    p = _run_hook({"session_id": "hook-sid", "hook_event_name": "PostToolUse",
                   "tool_name": "Bash"}, lockdir)
    assert p.returncode == 0
    assert session_lock.pc_active("hook-sid") is True


def test_stop_refreshes_not_releases(lockdir):
    _run_hook({"session_id": "hook-sid", "hook_event_name": "SessionStart"}, lockdir)
    _run_hook({"session_id": "hook-sid", "hook_event_name": "Stop"}, lockdir)
    assert session_lock.pc_active("hook-sid") is True


def test_sessionend_releases_heartbeat(lockdir):
    _run_hook({"session_id": "hook-sid", "hook_event_name": "SessionStart"}, lockdir)
    assert session_lock.pc_active("hook-sid") is True
    _run_hook({"session_id": "hook-sid", "hook_event_name": "SessionEnd"}, lockdir)
    assert session_lock.pc_active("hook-sid") is False


def test_tg_source_is_skipped(lockdir):
    p = _run_hook({"session_id": "tg-sid", "hook_event_name": "SessionStart"},
                  lockdir, extra_env={"ISLAND_AGENT_SOURCE": "tg"})
    assert p.returncode == 0
    assert session_lock.pc_active("tg-sid") is False


def test_missing_session_id_is_noop(lockdir):
    p = _run_hook({"hook_event_name": "SessionStart"}, lockdir)
    assert p.returncode == 0
    assert not os.path.exists(lockdir) or os.listdir(lockdir) == []


def test_garbage_stdin_is_noop(lockdir):
    env = dict(os.environ)
    env["TGLR_LOCK_DIR"] = lockdir
    p = subprocess.run(["bash", _HOOK], input="{ not json at all",
                       capture_output=True, text=True, env=env, timeout=20)
    assert p.returncode == 0
