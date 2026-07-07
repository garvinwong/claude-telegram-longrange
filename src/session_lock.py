#!/usr/bin/env python3
# session_lock.py — 会话跨进程互斥
#
# 目标：防止 PC 上的交互式 claude 与 Telegram 侧 `claude -p --resume` 并发写同一会话
#       .jsonl 造成 transcript 交错/损坏——这是"双向无缝接手"的核心稳定性隐患。
#
# 模型（软锁 + 硬持有混合，非绝对强锁）：
#   · TG 侧：对 <sid>.lock 持 fcntl.flock(LOCK_EX|LOCK_NB) 全程硬互斥。
#     覆盖 TG↔TG 及任何程序化 `claude -p` 路径；进程退出内核自动释放，无残留。
#   · PC 侧：交互式 CLI 无法被强制拦截，改由 SessionStart/PostToolUse/Stop hook
#     维护 <sid>.pc 心跳文件（含 ts）。TG 侧接手前读心跳，TTL 内视为"电脑正在用"，拒接手。
#   · 反向（PC 抢 TG 在跑会话）只能靠 SessionStart hook 告警，不能硬阻。
#
# 路径：LOCK_DIR 是固定目录——bash hook 与本模块必须约定同一目录，跨进程协调才成立。
#       经 TGLR_LOCK_DIR 覆盖（测试指向 tmp，绝不污染真实目录）。所有函数在调用时读
#       LOCK_DIR（非 import 期），使 monkeypatch/env 覆盖生效。

import fcntl
import json
import os
import time

# 固定目录；测试 monkeypatch 本变量或经 TGLR_LOCK_DIR 覆盖
LOCK_DIR = os.environ.get(
    "TGLR_LOCK_DIR", os.path.expanduser("~/.claude-telegram-longrange/locks"))

# PC 心跳新鲜度窗口（秒）。取 90s：远长于单次工具调用间隔，避免"思考中"误判空闲；
# 又短到 PC 会话真正结束后 TG 能较快接手。PostToolUse hook 每次工具调用刷新。
PC_TTL = 90.0


def _pc_path(sid):
    return os.path.join(LOCK_DIR, f"{sid}.pc")


def _flock_path(sid):
    return os.path.join(LOCK_DIR, f"{sid}.lock")


def pc_active(sid, ttl=PC_TTL, now=None):
    """PC 端是否正持有该会话（心跳在 TTL 内）。

    只读：不建目录、不写文件。文件缺失/损坏/无 ts 一律视为"未持有"（放行），
    宁可漏挡不可误锁——误锁会让用户永远接不了手，比残余并发窗口更伤体验。
    """
    if not sid:
        return False
    try:
        with open(_pc_path(sid), encoding="utf-8") as f:
            data = json.load(f)
        ts = float(data.get("ts", 0))
    except (OSError, ValueError, TypeError):
        return False
    now = time.time() if now is None else now
    return (now - ts) <= ttl


def touch_pc(sid, pid=None, now=None):
    """刷新/建立 PC 心跳。供 PC 侧 hook 与测试调用。best-effort。

    原子写（tmp + os.replace）防 pc_active 读到半截 JSON。任何 OSError 静默吞——
    心跳失败最坏是 TG 误判空闲接手，不应让 hook 报错影响交互会话。
    """
    if not sid:
        return
    now = time.time() if now is None else now
    try:
        os.makedirs(LOCK_DIR, exist_ok=True)
        tmp = _pc_path(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"owner": "pc", "pid": pid, "ts": now}, f)
        os.replace(tmp, _pc_path(sid))
    except OSError:
        pass


def release_pc(sid):
    """删除 PC 心跳（供 SessionEnd hook 与强制接手调用）。缺失按成功处理。"""
    if not sid:
        return
    try:
        os.remove(_pc_path(sid))
    except OSError:
        pass


def acquire_tg(sid):
    """TG 侧尝试拿会话独占锁。成功返回持有的 fd（须 release_tg 或进程退出释放），
    被占用返回 None。绝不阻塞（LOCK_NB）——拿不到即视为冲突，跳过本次运行。
    """
    if not sid:
        return None
    try:
        os.makedirs(LOCK_DIR, exist_ok=True)
        fd = os.open(_flock_path(sid), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"owner": "tg", "pid": os.getpid()}).encode())
    except OSError:
        pass
    return fd


def release_tg(fd):
    """释放 TG flock 并关 fd。fd 为 None 或已关按成功处理。"""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
