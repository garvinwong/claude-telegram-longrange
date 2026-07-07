#!/usr/bin/env python3
# test_session_lock.py — 会话跨进程互斥单测
#
# 覆盖：PC 心跳 TTL 判定、TG flock 互斥（同进程双 fd + 真跨进程 subprocess）、
#       runner 在被占用时跳过不起进程。全部用 tmp 目录（conftest autouse 隔离）。

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import session_lock  # noqa: E402
import runner        # noqa: E402


@pytest.fixture
def lockdir(tmp_path, monkeypatch):
    d = str(tmp_path / "locks")
    monkeypatch.setattr(session_lock, "LOCK_DIR", d)
    return d


def test_pc_active_absent_is_false(lockdir):
    assert session_lock.pc_active("sid-x") is False


def test_pc_active_fresh_true_stale_false(lockdir):
    session_lock.touch_pc("sid-x", now=1000.0)
    assert session_lock.pc_active("sid-x", now=1000.0 + 10) is True
    assert session_lock.pc_active("sid-x", now=1000.0 + 200) is False


def test_pc_active_garbage_file_is_false(lockdir):
    os.makedirs(lockdir, exist_ok=True)
    with open(os.path.join(lockdir, "sid-x.pc"), "w") as f:
        f.write("{ not json")
    assert session_lock.pc_active("sid-x") is False


def test_release_pc(lockdir):
    session_lock.touch_pc("sid-x")
    assert session_lock.pc_active("sid-x") is True
    session_lock.release_pc("sid-x")
    assert session_lock.pc_active("sid-x") is False


def test_pc_active_read_only_no_dir_creation(lockdir):
    session_lock.pc_active("sid-x")
    assert not os.path.exists(lockdir)


def test_empty_sid_is_noop(lockdir):
    assert session_lock.pc_active("") is False
    assert session_lock.acquire_tg("") is None
    session_lock.touch_pc("")
    session_lock.release_pc("")


def test_flock_mutual_exclusion_same_process(lockdir):
    fd1 = session_lock.acquire_tg("sid-y")
    assert fd1 is not None
    fd2 = session_lock.acquire_tg("sid-y")
    assert fd2 is None
    session_lock.release_tg(fd1)
    fd3 = session_lock.acquire_tg("sid-y")
    assert fd3 is not None
    session_lock.release_tg(fd3)


def test_flock_different_sids_independent(lockdir):
    fd1 = session_lock.acquire_tg("sid-a")
    fd2 = session_lock.acquire_tg("sid-b")
    assert fd1 is not None and fd2 is not None
    session_lock.release_tg(fd1)
    session_lock.release_tg(fd2)


def test_flock_mutual_exclusion_cross_process(lockdir):
    """子进程持锁期间，父进程 acquire 必失败——验证跨进程语义。"""
    import subprocess
    holder = (
        "import sys, time, session_lock\n"
        f"session_lock.LOCK_DIR = {lockdir!r}\n"
        "fd = session_lock.acquire_tg('sid-cross')\n"
        "sys.stdout.write('LOCKED\\n' if fd is not None else 'FAIL\\n'); sys.stdout.flush()\n"
        "time.sleep(2)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    p = subprocess.Popen([sys.executable, "-c", holder],
                         stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert p.stdout.readline().strip() == "LOCKED"
        assert session_lock.acquire_tg("sid-cross") is None
    finally:
        p.wait(timeout=5)
    fd = session_lock.acquire_tg("sid-cross")
    assert fd is not None
    session_lock.release_tg(fd)


def test_runner_skips_when_pc_active(lockdir):
    session_lock.touch_pc("sid-run")
    events = []
    out = runner._run(["false"], events.append, cwd="/tmp", timeout=5,
                      session_id="sid-run")
    assert out.get("locked") is True
    assert out.get("pid") is None
    assert not any(e.get("type") == "spawn" for e in events)
    assert any(e.get("type") == "error" and e.get("reason") == "locked"
               for e in events)


def test_runner_skips_when_flock_held(lockdir):
    held = session_lock.acquire_tg("sid-held")
    assert held is not None
    events = []
    out = runner._run(["false"], events.append, cwd="/tmp", timeout=5,
                      session_id="sid-held")
    assert out.get("locked") is True
    assert not any(e.get("type") == "spawn" for e in events)
    session_lock.release_tg(held)
