#!/usr/bin/env python3
# test_tasks_runner.py — 该阶段 单元/集成测试（pytest）
#
# 约束：绝不调用真实 claude、绝不耗额度——用动态生成的「假 claude 桩」按 argv/prompt
# 分派行为（正常吐 stream-json / 睡眠 / 非零退出）。台账全用 tmp_path 临时库，
# 绝不触碰 ~/.tg-longrange 与 ~/.agents-island。
#
# 运行：python3 -m pytest tests/test_tasks_runner.py -v

import json
import os
import stat
import sys
import threading
import time

import pytest

# 无包结构，把模块所在目录（src）挂上 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runner  # noqa: E402
import tasks    # noqa: E402


# ── 假 claude 桩 ─────────────────────────────────────────────────────────
# 依 prompt（argv 末元素）分派：含 SLEEP→长睡+起子进程（测取消/超时连坐）；
# 含 FAIL→写 stderr 非零退出（测 error）；否则吐 3 类合法事件 + 1 坏行 + 1 半行。
_STUB_SRC = r'''#!/usr/bin/env python3
import sys, os, json, time, subprocess

argv = sys.argv
af = os.environ.get("STUB_ARGV_FILE")
if af:
    with open(af, "w") as f:
        json.dump(argv, f)

sid = ""
for i, a in enumerate(argv):
    if a in ("--session-id", "--resume") and i + 1 < len(argv):
        sid = argv[i + 1]
prompt = argv[-1] if len(argv) > 1 else ""

if "SLEEP" in prompt:
    child = subprocess.Popen(["sleep", "120"])
    cf = os.environ.get("STUB_CHILD_FILE")
    if cf:
        open(cf, "w").write(str(child.pid))
    pf = os.environ.get("STUB_PGID_FILE")
    if pf:
        open(pf, "w").write(str(os.getpgrp()))
    time.sleep(120)
    sys.exit(0)

if "FAIL" in prompt:
    sys.stderr.write("boom: stub failure mode\n")
    sys.stderr.flush()
    sys.exit(3)

out = sys.stdout
def emit(o):
    out.write(json.dumps(o) + "\n"); out.flush()

emit({"type": "system", "subtype": "init", "session_id": sid})
emit({"type": "assistant", "message": {"role": "assistant",
      "content": [{"type": "text", "text": "hello from stub"}]}})
emit({"type": "assistant", "message": {"role": "assistant",
      "content": [{"type": "tool_use", "id": "tu1", "name": "Bash",
                   "input": {"command": "echo hi"}}]}})
out.write("{ this is not valid json }\n"); out.flush()          # 坏行
out.write('{"type":"assistant","message":{"role":"assi'); out.flush()  # 半行
out.write("\n")
emit({"type": "result", "subtype": "success", "is_error": False,
      "result": "done", "session_id": sid, "total_cost_usd": 0.0123,
      "permission_denials": [{"tool_name": "Bash",
                              "tool_input": {"command": "rm -rf ~"}}]})
sys.exit(0)
'''


@pytest.fixture
def stub(tmp_path, monkeypatch):
    p = tmp_path / "fake_claude.py"
    p.write_text(_STUB_SRC)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    # runner.CLAUDE_BIN 是导入期读定的模块常量，直接改属性才生效（env 已来不及）
    monkeypatch.setattr(runner, "CLAUDE_BIN", str(p))
    monkeypatch.setenv("TG_LR_CLAUDE_BIN", str(p))
    return str(p)


@pytest.fixture
def store(tmp_path):
    s = tasks.TaskStore(db_path=str(tmp_path / "tasks.db"))
    yield s
    s.close()


def _pid_gone(pid, tries=120):
    for _ in range(tries):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


def _pgid_gone(pgid, tries=120):
    for _ in range(tries):
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, OSError):
            return True
        time.sleep(0.05)
    return False


# ── ① 台账 CRUD 与状态流转 ────────────────────────────────────────────────
def test_crud_and_status_flow(store):
    tid = store.create(session_id="sid-1", title="任务甲", model="opusplan",
                        origin="new", chat_id="123456789")
    row = store.get(tid)
    assert row["status"] == tasks.STATUS_QUEUED
    assert row["title"] == "任务甲"
    assert row["created_at"] and row["updated_at"]

    prev_updated = row["updated_at"]
    time.sleep(0.01)
    store.update_status(tid, tasks.STATUS_RUNNING, last_event="已起进程")
    store.update_fields(tid, pid=12345, pgid=12345, progress_msg_id=99)
    row = store.get(tid)
    assert row["status"] == tasks.STATUS_RUNNING
    assert row["pid"] == 12345 and row["progress_msg_id"] == 99
    assert row["last_event"] == "已起进程"
    assert row["updated_at"] != prev_updated   # updated_at 每次写入自动刷新

    store.update_status(tid, tasks.STATUS_DONE)
    assert store.get(tid)["status"] == tasks.STATUS_DONE

    # list_recent 倒序、白名单外字段被拒
    store.create(session_id="sid-2", title="任务乙")
    recent = store.list_recent(limit=10)
    assert recent[0]["title"] == "任务乙"
    with pytest.raises(ValueError):
        store.update_fields(tid, created_at="伪造", bogus_col=1)  # 白名单外列被拒


# ── ② active_session_ids 只含活跃态 ───────────────────────────────────────
def test_active_session_ids(store):
    a = store.create(session_id="act-run", status=tasks.STATUS_RUNNING)
    store.create(session_id="act-queued", status=tasks.STATUS_QUEUED)
    store.create(session_id="act-wait", status=tasks.STATUS_WAITING_APPROVAL)
    store.create(session_id="dead-done", status=tasks.STATUS_DONE)
    store.create(session_id="dead-fail", status=tasks.STATUS_FAILED)
    store.create(session_id=None, status=tasks.STATUS_RUNNING)  # 无 sid 不计入

    active = store.active_session_ids()
    assert active == {"act-run", "act-queued", "act-wait"}
    assert "dead-done" not in active and "dead-fail" not in active
    _ = a


# ── ③ recover_on_start：PID 死→interrupted；PID 活→orphaned 保持 running ──
def test_recover_pid_dead_becomes_interrupted(store):
    # 起一个瞬时进程并等它退出，拿到一个「确定已死」的 PID
    import subprocess
    dead = subprocess.Popen(["true"])
    dead.wait()
    tid = store.create(session_id="s-dead", status=tasks.STATUS_RUNNING,
                       pid=dead.pid)
    res = store.recover_on_start()
    assert tid in res["interrupted"]
    assert store.get(tid)["status"] == tasks.STATUS_INTERRUPTED


def test_recover_pid_alive_becomes_orphaned(store):
    # 用当前测试进程自身 PID 冒充「仍活的孤儿」
    tid = store.create(session_id="s-alive", status=tasks.STATUS_RUNNING,
                       pid=os.getpid())
    res = store.recover_on_start()
    assert tid in res["orphaned"]
    row = store.get(tid)
    assert row["status"] == tasks.STATUS_RUNNING   # 活着→不改状态
    assert row["last_event"] == "orphaned"


# ── ④ 事件解析：3 类事件齐全 + 坏行/半行被跳过 ────────────────────────────
def test_event_parsing_skips_bad_lines(stub, tmp_path):
    events = []
    ret = runner.start("正常任务：回答问题", cwd=str(tmp_path),
                       on_event=events.append)
    types = [e["type"] for e in events]
    assert "assistant_text" in types
    assert "tool_use" in types
    assert "result" in types
    # spawn 事件必须最先到达且带 pid/pgid——daemon 运行中 /cancel 的唯一靶子来源
    assert types[0] == "spawn"
    spawn_ev = events[0]
    assert spawn_ev["pid"] and spawn_ev["pgid"] and spawn_ev["session_id"] == ret["session_id"]
    # 坏行 + 半行 各 1 → 至少 2 行被跳过，且未抛异常
    assert ret["bad_lines"] >= 2
    assert ret["returncode"] == 0

    text_ev = next(e for e in events if e["type"] == "assistant_text")
    assert text_ev["text"] == "hello from stub"
    tool_ev = next(e for e in events if e["type"] == "tool_use")
    assert tool_ev["tool"] == "Bash"
    assert "echo hi" in tool_ev["summary"]


# ── ⑤ result 事件透传 permission_denials ─────────────────────────────────
def test_result_permission_denials_passthrough(stub, tmp_path):
    events = []
    runner.start("正常任务", cwd=str(tmp_path), on_event=events.append)
    result_ev = next(e for e in events if e["type"] == "result")
    assert result_ev["ok"] is True
    assert result_ev["total_cost_usd"] == 0.0123
    assert result_ev["permission_denials"]
    assert result_ev["permission_denials"][0]["tool_name"] == "Bash"


# ── ⑥ cancel 杀进程组（子进程连坐）────────────────────────────────────────
def test_cancel_kills_process_group(stub, tmp_path, monkeypatch):
    pgid_file = tmp_path / "pgid.txt"
    child_file = tmp_path / "child.txt"
    monkeypatch.setenv("STUB_PGID_FILE", str(pgid_file))
    monkeypatch.setenv("STUB_CHILD_FILE", str(child_file))

    holder = {}

    def _run():
        holder["ret"] = runner.start("SLEEP 长任务", cwd=str(tmp_path),
                                     timeout=60, on_event=lambda e: None)

    t = threading.Thread(target=_run)
    t.start()

    # 等桩把自身 pgid 落盘（pgid 文件最后写，出现即两文件皆备）
    for _ in range(200):
        if pgid_file.exists() and pgid_file.read_text().strip():
            break
        time.sleep(0.05)
    assert pgid_file.exists() and pgid_file.read_text().strip(), "桩未起进程组"
    pgid = int(pgid_file.read_text().strip())
    child_pid = int(child_file.read_text().strip())

    runner.cancel(pgid, grace=2)
    t.join(timeout=15)
    assert not t.is_alive(), "runner 线程未随进程组终止而退出"
    assert _pid_gone(child_pid), "子进程未被进程组连坐杀死"
    assert _pgid_gone(pgid), "进程组仍存活"


# ── ⑦ 硬超时 → error(timeout) 且进程组死 ─────────────────────────────────
def test_hard_timeout(stub, tmp_path, monkeypatch):
    pgid_file = tmp_path / "pgid_to.txt"
    monkeypatch.setenv("STUB_PGID_FILE", str(pgid_file))
    events = []
    ret = runner.start("SLEEP 会超时的任务", cwd=str(tmp_path),
                       timeout=1.0, on_event=events.append)
    err = [e for e in events if e["type"] == "error"]
    assert err, "未收到 error 事件"
    assert err[0].get("reason") == "timeout"
    pgid = int(pgid_file.read_text().strip())
    assert _pgid_gone(pgid), "超时后进程组未被终止"
    _ = ret


# ── ⑧ model 出现在 argv 且位置正确、无 --dangerously-skip-permissions ─────
def test_model_argv_and_no_dangerous_flag(stub, tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("STUB_ARGV_FILE", str(argv_file))
    runner.start("正常任务", model="opusplan", cwd=str(tmp_path),
                 on_event=lambda e: None)
    argv = json.loads(argv_file.read_text())

    assert "--dangerously-skip-permissions" not in argv
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "opusplan"
    # --session-id 存在且带 uuid；prompt 恒为末元素
    assert "--session-id" in argv
    assert argv[-1] == "正常任务"
    # 基础安全参数就位
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert "stream-json" in argv


# ── 附加：resume 用 --resume 而非 --session-id ────────────────────────────
def test_resume_uses_resume_flag(stub, tmp_path, monkeypatch):
    argv_file = tmp_path / "argv_r.json"
    monkeypatch.setenv("STUB_ARGV_FILE", str(argv_file))
    runner.resume("existing-sid-xyz", "接力问题", cwd=str(tmp_path),
                  on_event=lambda e: None)
    argv = json.loads(argv_file.read_text())
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "existing-sid-xyz"
    assert "--session-id" not in argv


# ── 附加：非零退出 → error 事件 ───────────────────────────────────────────
def test_nonzero_exit_emits_error(stub, tmp_path):
    events = []
    ret = runner.start("FAIL 这个任务会失败", cwd=str(tmp_path),
                       on_event=events.append)
    assert ret["returncode"] == 3
    err = [e for e in events if e["type"] == "error"]
    assert err, "非零退出未产生 error 事件"
    assert "boom" in err[0]["message"]
