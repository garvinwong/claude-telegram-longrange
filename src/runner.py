#!/usr/bin/env python3
# runner.py — tg-longrange 会话运行器（子进程 + stream-json 事件流）
#
# 设计要点：
# 事实基础：该阶段 预研实测（CLI 2.1.200 实测）——
#   · -p --output-format json/stream-json 的 result 行含 session_id / permission_denials；
#   · -p --session-id 起会话、-p --resume 跨进程续接成立；
#   · -p 模式 PreToolUse hook 正常触发、无人应答 fail-closed。
#
# 安全红线（S2）：全程 argv 列表传参、shell=False；prompt 永远是 argv 的单个元素，
#                 绝不做任何字符串拼接进 shell；绝不出现 --dangerously-skip-permissions。

import json
import os
import signal
import subprocess
import threading
import time
import uuid

import session_lock   # 会话跨进程互斥：起进程前拿锁，防并发写坏 transcript

# 约束：可经环境变量覆盖，测试用假 claude 桩替换，避免消耗真实额度
CLAUDE_BIN = os.environ.get("TG_LR_CLAUDE_BIN", "claude")

# 工作区——resume 的会话文件按 cwd 落盘，cwd 必须与起会话时一致，否则 --resume
# 找不到会话。默认取 TGLR_WORKDIR 环境变量，否则当前工作目录。
DEFAULT_CWD = os.environ.get("TGLR_WORKDIR", os.getcwd())

DEFAULT_TIMEOUT = 7200.0        # 硬超时 2h
_TERM_GRACE = 5.0               # SIGTERM 后给进程组的收尾宽限，超时则 SIGKILL

_TOOL_SUMMARY_MAX = 200         # 工具入参摘要截断长度（审批卡/进度卡展示用）


def _truncate(text, limit):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _terminate_group(pgid, grace=_TERM_GRACE):
    """对进程组执行 SIGTERM → 等 grace 秒 → 仍活则 SIGKILL。

    约束：必须 killpg（负 pgid 语义）连坐子孙进程——claude 会派生工具子进程，
    只杀父进程会留下孤儿继续跑。进程组已不存在按成功处理。
    """
    if not pgid:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except OSError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def cancel(pgid, grace=_TERM_GRACE):
    # 供上层 /cancel 调用：与超时同款 TERM→KILL 序列
    _terminate_group(pgid, grace)


# ── 硬超时定时器表（/extend 运行中重挂）──────────────────────────────────────
# session_id -> {"timer": Timer, "on_timeout": fn}。runner 线程武装/撤除，
# /extend 从 daemon 线程重挂——全程 _TIMERS_LOCK 串行化，避免竞态。
_TIMERS = {}
_TIMERS_LOCK = threading.Lock()


def _arm_timer(session_id, timeout, on_timeout):
    timer = threading.Timer(timeout, on_timeout)
    timer.daemon = True
    with _TIMERS_LOCK:
        old = _TIMERS.get(session_id)
        if old:
            old["timer"].cancel()
        _TIMERS[session_id] = {"timer": timer, "on_timeout": on_timeout}
    timer.start()
    return timer


def _disarm_timer(session_id):
    with _TIMERS_LOCK:
        rec = _TIMERS.pop(session_id, None)
    if rec:
        rec["timer"].cancel()


def extend(session_id, seconds):
    """把在跑会话的硬超时从'现在'起重设为 seconds。成功返回 True。

    任务不在本进程运行（无注册定时器）返回 False。
    铁律：先武装新定时器、再撤旧的——保证任一瞬间至少一个已武装，绝不出现
          "撤了旧的、新的没起来"导致任务失去超时保护无限跑。
    """
    if not session_id or seconds <= 0:
        return False
    with _TIMERS_LOCK:
        rec = _TIMERS.get(session_id)
        if not rec:
            return False
        new_timer = threading.Timer(seconds, rec["on_timeout"])
        new_timer.daemon = True
        new_timer.start()
        rec["timer"].cancel()
        rec["timer"] = new_timer
    return True


def _child_env():
    # 约束：只在本子进程环境注入，不污染全局；ISLAND_HOOK_TIMEOUT=600 让 TG 任务
    # 审批有 10 分钟窗口
    env = dict(os.environ)
    env["ISLAND_AGENT_SOURCE"] = "tg"
    env["ISLAND_HOOK_TIMEOUT"] = "600"
    return env


def _build_argv(session_flag, sid, prompt, model):
    # 约束：prompt 恒为最后一个独立元素；shell=False 下不存在注入面（S2）
    argv = [
        CLAUDE_BIN, "-p",
        session_flag, sid,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "default",
    ]
    if model:
        argv += ["--model", model]   # model 已由上层枚举白名单校验后才传入（S2 ⑤）
    argv.append(prompt)
    return argv


def _emit(on_event, event):
    if on_event is not None:
        on_event(event)


def _parse_line(line, on_event):
    """宽容解析一行 stream-json，归一化后回调；坏行/半行跳过并计 1。

    约束：坏行绝不抛异常中断整条流——AI 输出偶发半行/
    非 JSON 噪声是常态，一行坏不能拖垮整个任务的事件采集。
    返回 (parsed_ok: bool, is_result: bool, result_obj_or_None)。
    """
    line = line.strip()
    if not line:
        return False, False, None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return False, False, None
    if not isinstance(obj, dict):
        return False, False, None

    etype = obj.get("type")
    if etype == "assistant":
        msg = obj.get("message") or {}
        for part in msg.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                _emit(on_event, {"type": "assistant_text",
                                 "text": part.get("text", "")})
            elif part.get("type") == "tool_use":
                _emit(on_event, {
                    "type": "tool_use",
                    "tool": part.get("name", ""),
                    "summary": _truncate(
                        json.dumps(part.get("input", {}), ensure_ascii=False),
                        _TOOL_SUMMARY_MAX),
                })
        return True, False, None

    if etype == "result":
        is_error = bool(obj.get("is_error"))
        result_text = obj.get("result", "")
        _emit(on_event, {
            "type": "result",
            "ok": not is_error,
            "result_text": result_text,
            # permission_denials 非空 = 有工具被 defer/拒
            "permission_denials": obj.get("permission_denials") or [],
            "session_id": obj.get("session_id"),
            "total_cost_usd": obj.get("total_cost_usd"),
        })
        if is_error:
            _emit(on_event, {"type": "error",
                             "message": result_text or "result is_error"})
        return True, True, obj

    # system/user/其他类型：非本层关注事件，静默忽略
    return True, False, None


def _run(argv, on_event, cwd, timeout, session_id):
    """阻塞式运行子进程并逐行分发事件——运行在调用方线程，由上层决定线程化。

    返回 {'session_id':..., 'pid':..., 'pgid':...}。返回时进程已结束
    （阻塞语义）；上层若需中途 cancel，应在起线程前另行掌握 pgid
    （或由子进程自曝其进程组，测试即用此法）。
    """
    # ── 会话互斥：起进程前拿锁 ──────────────────────────────────────────
    # ① PC 端正持有该会话（心跳 TTL 内）→ 跳过不起进程，防并发写坏 transcript。
    if session_lock.pc_active(session_id):
        _emit(on_event, {"type": "error", "reason": "locked",
                         "message": "该会话正在电脑上打开，已跳过以防写坏 transcript"})
        return {"session_id": session_id, "pid": None, "pgid": None,
                "returncode": None, "bad_lines": 0, "locked": True}
    # ② TG flock 拿不到（另一 `-p` 进程正在跑同一会话）→ 同样跳过。
    lock_fd = session_lock.acquire_tg(session_id)
    if lock_fd is None:
        _emit(on_event, {"type": "error", "reason": "locked",
                         "message": "该会话正被另一进程占用，已跳过以防写坏 transcript"})
        return {"session_id": session_id, "pid": None, "pgid": None,
                "returncode": None, "bad_lines": 0, "locked": True}
    try:
        return _run_locked(argv, on_event, cwd, timeout, session_id)
    finally:
        session_lock.release_tg(lock_fd)


def _run_locked(argv, on_event, cwd, timeout, session_id):
    """已持有会话 flock 的实际运行体（见 _run 的锁语义）。"""
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=_child_env(),
        text=True,
        bufsize=1,                 # 行缓冲：配合 text 模式让 readline 尽快拿到整行
        start_new_session=True,    # setsid：子进程自成会话+进程组，pgid==pid，便于整组回收
    )
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid

    # spawn 事件先于一切流事件：上层（daemon 线程）借此在任务运行中就拿到
    # pid/pgid 落台账，/cancel 才有靶子——阻塞语义下这是唯一的中途暴露通道
    _emit(on_event, {"type": "spawn", "session_id": session_id,
                     "pid": proc.pid, "pgid": pgid})

    # stderr 单独抽干，防止管道缓冲写满导致子进程阻塞（读 stdout 时的经典死锁）
    stderr_chunks = []

    def _drain_stderr():
        try:
            for eline in proc.stderr:
                stderr_chunks.append(eline)
        except (ValueError, OSError):
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    state = {"timed_out": False, "error_emitted": False}

    def _on_timeout():
        # 硬超时触发：整组 TERM→KILL，随后 stdout EOF 使读循环自然退出（R5）
        state["timed_out"] = True
        _terminate_group(pgid)

    # 注册到全局定时器表，使 /extend 能在运行中重挂硬超时
    _arm_timer(session_id, timeout, _on_timeout)

    bad_lines = 0
    try:
        # 约束：用 readline 而非 for-in 迭代——文本管道的迭代器有隐藏 read-ahead
        # 缓冲，会推迟单行到达，破坏进度卡的准实时性；readline 到 EOF 返回 ""
        while True:
            line = proc.stdout.readline()
            if line == "":
                break
            parsed_ok, is_result, _obj = _parse_line(line, on_event)
            if not parsed_ok:
                bad_lines += 1
                continue
            if is_result and _obj.get("is_error"):
                state["error_emitted"] = True
    finally:
        _disarm_timer(session_id)

    proc.wait()
    stderr_thread.join(timeout=1.0)
    stderr_text = "".join(stderr_chunks).strip()

    if state["timed_out"]:
        _emit(on_event, {"type": "error", "reason": "timeout",
                         "message": f"任务超过硬超时 {int(timeout)}s，进程组已终止"})
    elif proc.returncode not in (0, None) and stderr_text and not state["error_emitted"]:
        # 非零退出且有 stderr、且尚未由 result 行报过错 → 补发 error（避免重复）
        _emit(on_event, {"type": "error",
                         "message": _truncate(stderr_text, 1000)})

    return {"session_id": session_id, "pid": proc.pid, "pgid": pgid,
            "returncode": proc.returncode, "bad_lines": bad_lines}


def start(prompt, model=None, on_event=None, session_id=None, cwd=None,
          timeout=DEFAULT_TIMEOUT):
    """新起一个长程会话（-p --session-id <uuid>）。阻塞式，见 _run docstring。"""
    sid = session_id or str(uuid.uuid4())
    argv = _build_argv("--session-id", sid, prompt, model)
    return _run(argv, on_event, cwd or DEFAULT_CWD, timeout, sid)


def resume(sid, prompt, model=None, on_event=None, cwd=None,
           timeout=DEFAULT_TIMEOUT):
    """接力已有会话（-p --resume <sid>）。cwd 必须与起会话时一致（见 DEFAULT_CWD 注释）。"""
    argv = _build_argv("--resume", sid, prompt, model)
    return _run(argv, on_event, cwd or DEFAULT_CWD, timeout, sid)
