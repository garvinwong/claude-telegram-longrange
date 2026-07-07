#!/usr/bin/env bash
# session_lock_hook.sh — PC 侧会话心跳（配合 src/session_lock.py）
#
# 作用：由 Claude Code 的 SessionStart / PostToolUse / Stop / SessionEnd hook 调用，
#   维护 <lock_dir>/<sid>.pc 心跳文件，使 Telegram 侧在"电脑正在用该会话"时拒接手，
#   防 PC 交互式 claude 与 TG `claude -p --resume` 并发写坏同一 transcript。
#
# 协议：stdin = Claude Code hook JSON {"session_id":..., "hook_event_name":...}
#   · SessionEnd → 释放心跳（release_pc，会话真正结束）
#   · 其余一切（SessionStart / PostToolUse / Stop / …）→ 刷新心跳（touch_pc）
#     注意：交互模式 Stop 是"每轮回答结束"（非会话结束），故按刷新处理——否则两轮
#     对话之间锁被放掉，TG 会趁隙插入。会话真结束靠 TTL 自然过期 + SessionEnd 兜底。
#     刷新挂 PostToolUse（非 PreToolUse）：post 在工具执行后跑，绝不干扰审批决策链。
#
# 铁律：
#   ① 纯副作用、绝不向 stdout 输出任何内容——本 hook 可注册在 Pre/PostToolUse 等事件上，
#      任何 stdout 都可能被当作决定/上下文注入，污染主流程。全程静默。
#   ② best-effort：任何失败都 exit 0，绝不影响交互会话。
#   ③ TG 侧子进程（ISLAND_AGENT_SOURCE=tg）跳过——其锁由 daemon/runner 的 flock 直管。
#
# 接线（示例，加入 ~/.claude/settings.json，与已有 hook 并列不覆盖）：
#   SessionStart / PostToolUse / Stop / SessionEnd 各挂一条 command 指向本脚本。
#
# 路径：src 目录默认相对本脚本解析（../src），可经 TGLR_SRC_DIR 覆盖。

SL_DIR="${TGLR_SRC_DIR:-$(cd "$(dirname "$0")/../src" 2>/dev/null && pwd)}"

# TG 侧 -p 子进程不参与 PC 心跳
if [ "${ISLAND_AGENT_SOURCE:-}" = "tg" ]; then
    cat >/dev/null 2>&1 || true
    exit 0
fi

INPUT="$(cat 2>/dev/null || true)"

HOOK_INPUT="$INPUT" SL_DIR="$SL_DIR" python3 -c '
import json, os, sys
sys.path.insert(0, os.environ.get("SL_DIR", ""))
try:
    import session_lock
    data = json.loads(os.environ.get("HOOK_INPUT") or "{}")
    sid = (data.get("session_id") or "").strip()
    event = data.get("hook_event_name") or ""
    if sid:
        if event == "SessionEnd":
            session_lock.release_pc(sid)
        else:
            session_lock.touch_pc(sid, pid=os.getpid())
except Exception:
    pass
' >/dev/null 2>&1 || true

exit 0
