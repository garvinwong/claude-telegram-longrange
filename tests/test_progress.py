#!/usr/bin/env python3
# test_progress.py — 该阶段 进度卡单元测试（pytest，全 mock，假时钟）
#
# 约束：绝不做真实网络调用——send/edit 全用 Fake 记录调用与文本；
# 时钟用可拨动假时钟（now_fn），禁止 time.sleep 测节流（用注入时钟推进）。
#
# 运行：python3 -m pytest tests/test_progress.py -v

import os
import sys

import pytest

# 无包结构：把模块所在目录（src）挂上 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import progress  # noqa: E402


# ── 测试替身 ─────────────────────────────────────────────────────────────
class FakeSend:
    """记录 sendMessage 调用；返回递增 message_id。"""

    def __init__(self):
        self.calls = []      # [(chat_id, text), ...]
        self._mid = 1000

    def __call__(self, chat_id, text):
        self.calls.append((chat_id, text))
        self._mid += 1
        return self._mid


class FakeEdit:
    """记录 editMessageText 调用；按预置队列返回响应，队列空则默认 ok。"""

    def __init__(self):
        self.calls = []          # [(chat_id, message_id, text), ...]
        self.responses = []      # 预置响应队列 [{'ok':..,'retry_after':..}, ...]

    def __call__(self, chat_id, message_id, text):
        self.calls.append((chat_id, message_id, text))
        if self.responses:
            return self.responses.pop(0)
        return {"ok": True, "retry_after": None}


class Clock:
    """可拨动假时钟，作为 now_fn 注入——单调递增由测试手动 advance。"""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def clock():
    return Clock()


def _make(send, edit, clock, **kw):
    kw.setdefault("throttle_s", 20)
    return progress.ProgressCard(
        send_fn=send, edit_fn=edit, chat_id=42, task_id=7,
        title=kw.pop("title", "测试任务标题"),
        session_id=kw.pop("session_id", "sid-abc-123"),
        now_fn=clock, **kw)


# ── ① 节流窗口内多次 tool_use 只编辑一次，窗口过后补编（T-R4b 语义） ──────
def test_throttle_window_coalesces_then_flushes(clock):
    send, edit = FakeSend(), FakeEdit()
    card = _make(send, edit, clock, throttle_s=20)

    card.handle_event({"type": "tool_use", "tool": "Read", "summary": "a.py"})
    assert len(edit.calls) == 1          # 首个 tool_use：last_edit_at 为空，直接编辑

    # 同一 20s 窗口内再来两次 → 仅暂存，不再调 edit_fn
    card.handle_event({"type": "tool_use", "tool": "Grep", "summary": "foo"})
    card.handle_event({"type": "tool_use", "tool": "Edit", "summary": "bar"})
    assert len(edit.calls) == 1

    # 窗口过后再来一次 → 补编（第 2 次），且渲染的是最新状态
    clock.advance(21)
    card.handle_event({"type": "tool_use", "tool": "Bash", "summary": "ls"})
    assert len(edit.calls) == 2
    assert "Bash" in edit.calls[-1][2]


# ── ② 关键事件（error/result）穿透节流立即编辑 ────────────────────────────
def test_key_events_bypass_throttle(clock):
    send, edit = FakeSend(), FakeEdit()
    card = _make(send, edit, clock, throttle_s=20)

    card.handle_event({"type": "tool_use", "tool": "Read", "summary": "x"})
    assert len(edit.calls) == 1

    # 仍在 20s 窗口内，但 error / result 属关键事件，必立即编辑
    card.handle_event({"type": "error", "message": "boom"})
    assert len(edit.calls) == 2
    card.handle_event({"type": "result", "ok": True, "permission_denials": []})
    assert len(edit.calls) == 3


# ── ③ 429：retry_after=5 → 期间不再调 edit_fn，拨过 5s 后补编 ──────────────
def test_429_backoff_stages_then_resumes(clock):
    send, edit = FakeSend(), FakeEdit()
    edit.responses = [{"ok": False, "retry_after": 5}]  # 首次编辑撞 429
    card = _make(send, edit, clock, throttle_s=20)

    # error 触发一次编辑 → 收到 429，记 not_before = now + 5
    card.handle_event({"type": "error", "message": "first"})
    assert len(edit.calls) == 1

    # 退避期内即便关键事件也只暂存，绝不再调 edit_fn
    card.handle_event({"type": "result", "ok": True, "permission_denials": []})
    card.handle_event({"type": "error", "message": "second"})
    assert len(edit.calls) == 1

    # 拨过 5s → 下一次事件补编（第 2 次），恢复正常
    clock.advance(6)
    card.handle_event({"type": "tool_use", "tool": "Read", "summary": "z"})
    assert len(edit.calls) == 2


# ── ④ 超长文本截断至 ≤4000 且 sid 仍在文本中 ─────────────────────────────
def test_oversize_truncation_keeps_sid(clock):
    send, edit = FakeSend(), FakeEdit()
    # 构造一个逼近上限的长 sid（<4000），验证节点区被砍、sid 绝不被截
    long_sid = "s" * 3800
    card = _make(send, edit, clock, session_id=long_sid)

    # 灌入若干长节点，使 frame+节点 越过 4000，触发保底截断
    for i in range(3):
        card.handle_event({"type": "tool_use", "tool": "Bash",
                           "summary": "x" * 200})
    # 关键事件强制一次编辑，取最终卡片文本
    card.handle_event({"type": "error", "message": "e" * 200})

    text = edit.calls[-1][2]
    assert len(text) <= 4000
    assert long_sid in text          # sid 全码绝不被截（终端接管靠它）


# ── ⑤ 状态机：构造→running 骨架→finish(done) 终态无 ⚠️ 且另发新消息 ───────
def test_state_machine_and_finish_notify(clock):
    send, edit = FakeSend(), FakeEdit()
    card = _make(send, edit, clock)

    # 构造即发初始卡（send 1 次）；running 骨架含 ⚠️ 与 running emoji
    assert len(send.calls) == 1
    initial = send.calls[0][1]
    assert "⚠️" in initial
    assert "🏃" in initial
    assert card.message_id is not None

    card.finish("done")
    # 终态卡：最后一次编辑去掉 ⚠️ 行、状态行换 ✅
    last_edit_text = edit.calls[-1][2]
    assert "⚠️" not in last_edit_text
    assert "✅" in last_edit_text
    # 另发一条新消息触发手机通知（send 累计 2 次）
    assert len(send.calls) == 2
    assert "完成" in send.calls[-1][1]


# ── 状态草稿 vs 最终答案分离 ───────────────────────────────────────
def test_final_answer_sent_as_separate_message(clock):
    send, edit = FakeSend(), FakeEdit()
    card = _make(send, edit, clock)
    # 收到 result 事件（含最终答案全文）
    answer = "这是最终答案：周报提纲一、二、三……" * 3
    card.handle_event({"type": "result", "ok": True, "result_text": answer,
                       "permission_denials": []})
    card.finish("done")
    # 进度卡（编辑）保持紧凑状态草稿，不含答案全文
    assert answer not in edit.calls[-1][2]
    # 最终答案作为独立新消息发出（含答案文本 + 完成头），触发通知
    joined = "\n".join(c[1] for c in send.calls)
    assert answer in joined and "任务 #7 完成" in joined


def test_long_final_answer_chunked(clock):
    send, edit = FakeSend(), FakeEdit()
    card = _make(send, edit, clock)
    answer = "x" * 9000               # 超 4000，应分段
    card.handle_event({"type": "result", "ok": True, "result_text": answer,
                       "permission_denials": []})
    n_before = len(send.calls)
    card.finish("done")
    new_msgs = [c[1] for c in send.calls[n_before:]]
    assert len(new_msgs) >= 3          # 9000 字至少 3 段
    assert all(len(m) <= 4000 + 20 for m in new_msgs)   # 每段不超上限(+头部裕量)


# ── ⑥ finish(interrupted, note=...) 通知文本含 note ──────────────────────
def test_finish_interrupted_note_in_notice(clock):
    send, edit = FakeSend(), FakeEdit()
    card = _make(send, edit, clock)

    note = "审批超时中断，可 /say 3 继续"
    card.finish("interrupted", note=note)
    assert note in send.calls[-1][1]
    assert "中断" in send.calls[-1][1]
    # 终态卡状态行换 interrupted emoji、无 ⚠️
    assert "⏸️" in edit.calls[-1][2]
    assert "⚠️" not in edit.calls[-1][2]


# ── ⑦ permission_denials 非空的 result → 卡片含 ⛔ 节点 ────────────────────
def test_result_with_denials_shows_block_node(clock):
    send, edit = FakeSend(), FakeEdit()
    card = _make(send, edit, clock)

    card.handle_event({"type": "result", "ok": True,
                       "permission_denials": [{"tool": "Bash"}]})
    text = edit.calls[-1][2]
    assert "⛔" in text
    assert "✅ 完成" in text          # ok=True 同时给出完成节点
