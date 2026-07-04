#!/usr/bin/env python3
# progress.py — tg-longrange 进度回传（Telegram 单条进度卡）
#
# 设计要点：
# 设计红线：
#   · 纯 stdlib——不 import requests、不 import daemon；网络能力（send/edit）全部由构造注入，
#     便于单元测试全 mock、不做任何真实网络调用。
#   · 单卡防刷屏：稳态用 editMessageText 静默刷新（编辑不触发手机通知）；
#     只有终态另发一条新消息触发通知（关键节点必达）——两全之策。
#   · 节流：距上次成功编辑 < throttle_s 只更新内部状态不调 edit_fn；
#     关键事件（error/result/finish）穿透节流立即编辑。
#   · 429 退避：edit_fn 返回 retry_after → 记 not_before，退避期一切编辑
#     （含关键事件）改为暂存，到点后下一次 handle_event/finish 补编。
#   · sid 绝不许被截：终端接管（claude --resume）全靠它，超长时先砍节点区保 sid。
#   · 线程安全：handle_event 可能由 runner 线程调、finish 由 daemon 线程调，
#     内部状态一律 threading.Lock 保护。

import threading
import time

# 卡片文本总长上限——Telegram 单条消息硬限约 4096，留裕量取 4000（超长先砍节点区）
_CARD_MAX = 4000

# 状态 emoji：running 与五个终态。终态卡去掉 ⚠️ 行、状态行换对应 emoji。
_STATUS_EMOJI = {
    "running": "🏃",
    "done": "✅",
    "failed": "❌",
    "cancelled": "🚫",
    "interrupted": "⏸️",
    "waiting_approval": "⏳",
}

# 终态短通知里的中文措辞
_STATUS_LABEL = {
    "done": "完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "中断",
    "waiting_approval": "待审批",
}


def _clip(text, limit):
    # 约束：截断保留省略号且总长 ≤ limit（title 40 / 节点 80 都靠它兜边界）
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _chunk(text, size):
    # 最终答案超 Telegram 单条上限时按 size 硬切成多段（不丢内容，与卡片的截断不同）
    text = text or ""
    if len(text) <= size:
        return [text]
    return [text[i:i + size] for i in range(0, len(text), size)]


class ProgressCard:
    """一个长程任务对应一张 Telegram 进度卡（单条消息，持续 edit 刷新）。

    构造契约：
        send_fn(chat_id, text) -> message_id|None
        edit_fn(chat_id, message_id, text) -> {'ok': bool, 'retry_after': float|None}
    构造时立即 send_fn 发一条初始卡，message_id 存 self.message_id。
    """

    def __init__(self, send_fn, edit_fn, chat_id, task_id, title,
                 session_id, model=None, throttle_s=20, now_fn=time.monotonic):
        self.send_fn = send_fn
        self.edit_fn = edit_fn
        self.chat_id = chat_id
        self.task_id = task_id
        self.title = _clip(title, 40)          # 标题截 40 字（P2 移动端可读）
        self.session_id = session_id           # 全码，终端接管用，绝不截
        self.model = model or "默认"
        self.throttle_s = throttle_s
        self.now_fn = now_fn                    # 注入时钟：测试可控，禁真实 sleep 测节流

        self.lock = threading.Lock()
        self.nodes = []                         # 关键节点文本（最新在尾，渲染取末 3 条）
        self.status = "running"
        self.finished = False
        self.last_edit_at = None                # 上次成功编辑的时刻（None=尚未编辑）
        self.not_before = 0.0                   # 429 退避截止时刻（now < 此值则暂存）
        self.dirty = False                      # 有未落盘的状态变更（供下轮补编）
        self.start_time = now_fn()
        self._final_text = None                 # 最终答案全文（result 事件捕获，finish 时另发）

        # 构造即发初始卡：send 触发一次通知，之后一律 edit 静默刷新（单卡防刷屏）
        self.message_id = self.send_fn(self.chat_id, self._render())

    # ── 事件消费 ────────────────────────────────────────────────────────
    def handle_event(self, event):
        """消费 runner 标准事件（spawn/assistant_text/tool_use/result/error）。"""
        with self.lock:
            if self.finished:
                return  # 终态后一切迟到事件丢弃，避免覆盖终态卡
            etype = event.get("type")
            if etype == "result":
                # 捕获最终答案全文——进度卡只放状态/节点（草稿），答案在 finish 时单独成消息
                self._final_text = (event.get("result_text") or "").strip()
            new_nodes = self._nodes_from_event(event)
            for node in new_nodes:
                self.nodes.append(node)
            # 关键事件穿透节流：error/result 必立即编辑（退避期仍受 not_before 约束）
            key = etype in ("error", "result")
            if not new_nodes and not key:
                return  # spawn / 未知类型：卡片无需变化，不浪费一次编辑
            self._maybe_edit(force=key)

    def finish(self, status, note=None):
        """终态：最后一次编辑卡片（去 ⚠️ 行）+ 另发一条短通知触发手机通知。

        status ∈ done|failed|cancelled|interrupted|waiting_approval。
        """
        with self.lock:
            if self.finished:
                return
            self.finished = True
            self.status = status
            now = self.now_fn()
            # 终态卡编辑穿透节流；但仍受 429 退避约束——退避期暂存，
            # 靠随后那条新消息通知兜底送达（编辑丢了不致命，通知必达）
            if now >= self.not_before:
                self._do_edit(now)
            else:
                self.dirty = True
            # 状态草稿 vs 最终答案分离（借鉴 OpenClaw progress 模式）：
            # 进度卡是"状态草稿"（就地编辑、不触发通知）；完成时最终答案作独立新消息发出
            # （触发手机通知，且是可读的交付物，不被进度节点淹没/覆盖）。
            label = _STATUS_LABEL.get(status, status)
            emoji = _STATUS_EMOJI.get(status, "")
            if status == "done" and self._final_text:
                # 成功且有答案：标题头 + 全文（超长分段，末段附完成标记）
                head = f"🏁 任务 #{self.task_id} 完成"
                chunks = _chunk(self._final_text, _CARD_MAX)
                self.send_fn(self.chat_id, f"{head}\n\n{chunks[0]}")
                for extra in chunks[1:]:
                    self.send_fn(self.chat_id, extra)
            else:
                # 失败/取消/中断/无答案：发简短状态通知（note 优先，回退标题）
                note_text = note if note else self.title
                self.send_fn(self.chat_id,
                             f"{emoji} 任务 #{self.task_id} {label}：{note_text}".strip())

    # ── 内部：节点归一化 ─────────────────────────────────────────────────
    def _nodes_from_event(self, event):
        # 约束：节点文案与 设计 规格逐条对齐；每条截 80 字
        etype = event.get("type")
        if etype == "tool_use":
            line = f"🔧 {event.get('tool', '')} {event.get('summary', '')}".strip()
            return [_clip(line, 80)]
        if etype == "assistant_text":
            text = (event.get("text") or "").strip()
            if not text:
                return []
            return [_clip(f"💬 {text[:40]}", 80)]
        if etype == "error":
            return [_clip(f"❌ {event.get('message', '')}", 80)]
        if etype == "result":
            out = []
            if event.get("ok"):
                out.append("✅ 完成")
            # permission_denials 非空 = 有工具审批被拒/超时（runner 已归一化该字段）
            if event.get("permission_denials"):
                out.append("⛔ 有工具审批被拒/超时")
            return out
        return []  # spawn / 未知类型

    # ── 内部：编辑调度（节流 + 429 退避） ───────────────────────────────
    def _maybe_edit(self, force):
        now = self.now_fn()
        # 退避优先级最高：期间连关键事件也只暂存
        if now < self.not_before:
            self.dirty = True
            return
        # 非关键事件受节流：窗口内只更新内部状态，标脏留待补编
        if (not force and self.last_edit_at is not None
                and (now - self.last_edit_at) < self.throttle_s):
            self.dirty = True
            return
        self._do_edit(now)

    def _do_edit(self, now):
        if self.message_id is None:
            self.dirty = True  # 初始发送失败，无卡可编辑，留待将来（不致命）
            return
        text = self._render()
        resp = self.edit_fn(self.chat_id, self.message_id, text) or {}
        if resp.get("ok"):
            self.last_edit_at = now
            self.dirty = False
        elif resp.get("retry_after") is not None:
            # 429：记退避截止点，本次内容暂存，到点后补编
            self.not_before = now + resp["retry_after"]
            self.dirty = True
        else:
            # 普通编辑失败不致命，下轮再编（R4）
            self.dirty = True

    # ── 内部：卡片渲染 ───────────────────────────────────────────────────
    def _elapsed_str(self):
        secs = int(max(0, self.now_fn() - self.start_time))
        return f"{secs // 60:02d}:{secs % 60:02d}"

    def _render(self):
        terminal = self.finished
        emoji = _STATUS_EMOJI.get(self.status, "🏃")
        header = f"{emoji} 任务 #{self.task_id} {self.title}"
        info = f"模型: {self.model} | 已用时: {self._elapsed_str()}"
        node_header = "── 最近节点 ──"
        # sid 区固定放最后，超长时它绝不被砍（终端接管靠它）
        sid_block = f"── sid（终端接管用）──\n{self.session_id}"
        warn = None if terminal else "⚠️ running 中请勿在终端同时 resume"

        recent = list(self.nodes[-3:])  # 最新在下，最多 3 条

        def assemble(nodes):
            body = "\n".join(f"· {n}" for n in nodes) if nodes else "· （暂无节点）"
            parts = [header, info, node_header, body, sid_block]
            if warn:
                parts.append(warn)
            return "\n".join(parts)

        text = assemble(recent)
        # 超长保护：sid 区绝不截，逐条丢弃最旧节点直至 ≤ 上限（保底状态行 + sid 区）
        while len(text) > _CARD_MAX and recent:
            recent.pop(0)
            text = assemble(recent)
        return text
