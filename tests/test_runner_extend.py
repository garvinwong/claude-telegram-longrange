#!/usr/bin/env python3
# test_runner_extend.py — 硬超时定时器重挂（/extend）单测

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import runner  # noqa: E402


def teardown_function(_):
    for sid in list(runner._TIMERS.keys()):
        runner._disarm_timer(sid)


def test_arm_then_disarm_no_fire():
    fired = threading.Event()
    runner._arm_timer("s1", 0.3, fired.set)
    runner._disarm_timer("s1")
    assert not fired.wait(0.5)
    assert "s1" not in runner._TIMERS


def test_extend_cancels_old_fires_new():
    fired = []
    runner._arm_timer("s2", 0.3, lambda: fired.append("t"))
    ok = runner.extend("s2", 1.0)
    assert ok is True
    time.sleep(0.5)
    assert fired == [], "旧定时器应已被撤销"
    time.sleep(0.8)
    assert fired == ["t"], "新定时器应按新时长触发一次"
    runner._disarm_timer("s2")


def test_extend_unknown_session_returns_false():
    assert runner.extend("nonexistent", 5.0) is False


def test_extend_rejects_nonpositive():
    runner._arm_timer("s3", 5.0, lambda: None)
    assert runner.extend("s3", 0) is False
    assert runner.extend("s3", -1) is False
    runner._disarm_timer("s3")


def test_arm_replaces_stale_same_sid():
    a = []
    runner._arm_timer("s4", 0.3, lambda: a.append("old"))
    runner._arm_timer("s4", 0.3, lambda: a.append("new"))
    time.sleep(0.5)
    assert a == ["new"]
    runner._disarm_timer("s4")
