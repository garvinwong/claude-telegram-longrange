#!/usr/bin/env python3
# test_approval_relay.py — 该阶段 审批中继单测（pytest）
#
# 约束（红线）：桥客户端 + TG API 全替身，绝不联网、绝不碰 5599 生产桥；
#   降级路径用 tmp_path 造 queue/responses，绝不写真实 ~/.agents-island/。DB 用 tmp_path。
#
# 运行：python3 -m pytest tests/test_approval_relay.py -v

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import approval_relay as ar   # noqa: E402
import config   # noqa: E402
import tasks     # noqa: E402

WHITELIST_UID = 123456789


# ── 替身 ─────────────────────────────────────────────────────────────────
class FakeApi:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.answered = []
        self._mid = 500

    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML"):
        self._mid += 1
        self.sent.append({"chat_id": chat_id, "text": text,
                          "reply_markup": reply_markup, "mid": self._mid})
        return self._mid

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append({"message_id": message_id, "text": text})
        return {"ok": True, "retry_after": None}

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered.append({"id": callback_query_id, "text": text})


class FakeBridge:
    """可编排的桥替身：get_state 返回预置状态；post_decision 返回预置结果。"""

    def __init__(self):
        self.state = {"pending": []}
        self.unreachable = False        # True → get_state/post_decision 返回 None（降级）
        self.post_result = True
        self.posted = []

    def get_state(self):
        return None if self.unreachable else self.state

    def post_decision(self, perm_id, decision, reason=""):
        self.posted.append({"id": perm_id, "decision": decision, "reason": reason})
        if self.unreachable:
            return None
        return self.post_result


@pytest.fixture
def relay(tmp_path, monkeypatch):
    # 白名单来自 env/config（测试环境为空）→ 显式置入测试 UID，否则 callback 全被拦
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {WHITELIST_UID})
    store = tasks.TaskStore(db_path=str(tmp_path / "tasks.db"))
    api = FakeApi()
    bridge = FakeBridge()
    state_dir = tmp_path / "island"
    (state_dir / "responses").mkdir(parents=True)
    fch = ar.FileChannel(str(state_dir))
    r = ar.ApprovalRelay(api, store, config, bridge=bridge, file_channel=fch,
                         poll_interval=0.01)
    yield r, api, bridge, store, fch
    r.shutdown()
    store.close()


def _pending(eid, sid, tool="Bash", cmd="ls -la", agent="tg"):
    return {"id": eid, "session_id": sid, "tool_name": tool,
            "tool_input": {"command": cmd}, "agent_source": agent,
            "title": "测试任务"}


def _active_task(store, sid):
    return store.create(session_id=sid, title="活跃任务",
                        status=tasks.STATUS_RUNNING, chat_id=str(WHITELIST_UID))


# ── ① pending（会话在台账活跃集）→ 推 inline keyboard ─────────────────────
def test_pending_pushes_keyboard(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_pending("perm1", "sid-A")]}
    r.poll_once()
    assert len(api.sent) == 1
    kb = api.sent[0]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["✅ 批准", "❌ 拒绝", "⚡ 全批"]
    # callback_data 含 perm_id、格式正确、< 64 字节
    for b in kb:
        assert b["callback_data"].startswith("tglr:perm1:")
        assert len(b["callback_data"].encode()) < 64


# ── ② 本机开发会话（不在台账）→ 绝不推 TG（S1/C3 主过滤）──────────────────
def test_non_tracked_session_not_pushed(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    # pending 属于另一个未登记会话（本机终端开发会话）
    bridge.state = {"pending": [_pending("permX", "sid-LOCAL-DEV")]}
    r.poll_once()
    assert api.sent == []   # 一条都不推


# ── ③ allow 回注：POST /api/decision + answer + 编辑结果态 ──────────────────
def test_allow_commits_via_bridge(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_pending("perm1", "sid-A")]}
    r.poll_once()
    cq = {"id": "cbq1", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data("perm1", "allow")}
    assert r.handle_callback(cq) is True
    assert bridge.posted == [{"id": "perm1", "decision": "allow", "reason": ""}]
    assert api.answered[-1]["text"] == "✅ 已批准"
    assert any("已批准" in e["text"] for e in api.edited)


# ── ④ deny 带 reason 回注 ─────────────────────────────────────────────────
def test_deny_carries_reason(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_pending("perm1", "sid-A")]}
    r.poll_once()
    cq = {"id": "cbq", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data("perm1", "deny")}
    r.handle_callback(cq)
    assert bridge.posted[-1]["decision"] == "deny"
    assert bridge.posted[-1]["reason"]   # 非空 reason 传回模型


# ── ⑤ 伪造 from.id 的 callback → 静默丢弃、不落桥（S1）──────────────────────
def test_forged_from_id_rejected(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_pending("perm1", "sid-A")]}
    r.poll_once()
    cq = {"id": "cbqX", "from": {"id": 424242},
          "data": ar.build_callback_data("perm1", "allow")}
    assert r.handle_callback(cq) is True
    assert bridge.posted == []             # 决不落桥
    assert api.answered[-1]["text"] is None  # 静默 answer


# ── ⑥ 桥宕 → 降级：直读队列 pending + 直写响应文件 ────────────────────────
def test_bridge_down_degrades_to_files(relay, tmp_path):
    r, api, bridge, store, fch = relay
    _active_task(store, "sid-A")
    bridge.unreachable = True
    # 造队列文件：一条属活跃会话的待审批
    with open(fch.queue_file, "w", encoding="utf-8") as f:
        f.write(_json_line(_pending("permD", "sid-A")))
    r.poll_once()
    assert len(api.sent) == 1   # 降级也能推卡（卡上标注降级模式）
    assert "降级" in api.sent[0]["text"]
    # allow → 直写响应文件（桥不可达，post_decision 返回 None）
    cq = {"id": "c", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data("permD", "allow")}
    r.handle_callback(cq)
    resp_path = os.path.join(fch.resp_dir, "permD.json")
    assert os.path.exists(resp_path)
    import json
    assert json.load(open(resp_path))["decision"] == "allow"


# ── ⑦ 先应者赢：pending 消失（岛先批）→ 编辑为「已在本机处理」──────────────
def test_first_responder_wins_edit(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_pending("perm1", "sid-A")]}
    r.poll_once()                       # 推卡
    bridge.state = {"pending": []}      # 岛已批，条目从桥消失
    r.poll_once()                       # 应检测到消失并编辑
    assert any("已在本机" in e["text"] for e in api.edited)
    # 此后 Owner 再点该按钮 → 已答，回过期、不落桥
    cq = {"id": "late", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data("perm1", "allow")}
    r.handle_callback(cq)
    assert bridge.posted == []
    assert "过期" in api.answered[-1]["text"] or "已处理" in api.answered[-1]["text"]


# ── ⑧ 幽灵批准防护：对不在途的 perm_id 点击 → 拒绝、不写响应（S4）──────────
def test_ghost_approval_rejected(relay):
    r, api, bridge, store, fch = relay
    _active_task(store, "sid-A")
    # 从未推过任何卡；直接构造一个 callback
    cq = {"id": "ghost", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data("nonexistent", "allow")}
    assert r.handle_callback(cq) is True
    assert bridge.posted == []
    assert not os.path.exists(os.path.join(fch.resp_dir, "nonexistent.json"))
    assert "过期" in api.answered[-1]["text"] or "已处理" in api.answered[-1]["text"]


# ── ⑨ 重复点击同一按钮 → 第二次仅提示已处理、无第二次 POST（S4 重放）────────
def test_double_click_no_second_post(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_pending("perm1", "sid-A")]}
    r.poll_once()
    cq = {"id": "c1", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data("perm1", "allow")}
    r.handle_callback(cq)
    n_edits = len(api.edited)
    r.handle_callback(cq)   # 第二次
    assert len(bridge.posted) == 1   # 只落一次桥
    assert len(api.edited) == n_edits  # 第二次不再改写消息（已 answered）


# ── 批次C：点击后审批卡改写为结果态，保留工具上下文、撤掉按钮 ────────────────
def test_approval_click_collapses_with_tool_context(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_pending("perm1", "sid-A", tool="Bash", cmd="rm x")]}
    r.poll_once()
    cq = {"id": "c", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data("perm1", "allow")}
    r.handle_callback(cq)
    last = api.edited[-1]
    assert "已批准" in last["text"] and "Bash" in last["text"]  # 保留工具上下文
    # edit_message 未带 reply_markup → Telegram 撤掉按钮（防重复点）
    assert "reply_markup" not in last or last.get("reply_markup") is None


# ── ⑩ callback_data 编解码往返 + 非本中继 data 拒解析 ─────────────────────
def test_callback_data_roundtrip():
    for action in ("allow", "deny", "always"):
        data = ar.build_callback_data("abc123def456_1783000000", action)
        assert ar.parse_callback_data(data) == ("abc123def456_1783000000", action)
    # 非本中继前缀 / 非法动作 → None
    assert ar.parse_callback_data("other:x:allow") is None
    assert ar.parse_callback_data("tglr:x:bogus") is None
    assert ar.parse_callback_data(None) is None


# ── ⑪ always → POST decision=always（桥）；降级则写 allow+always 标志 ───────
def test_always_bridge_and_degrade(relay):
    r, api, bridge, store, fch = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_pending("permA", "sid-A", agent="tg")]}
    r.poll_once()
    cq = {"id": "c", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data("permA", "always")}
    r.handle_callback(cq)
    assert bridge.posted[-1]["decision"] == "always"

    # 降级路径：always → 写 allow 响应 + always_<agent> 标志
    bridge.unreachable = True
    _active_task(store, "sid-B")
    with open(fch.queue_file, "w", encoding="utf-8") as f:
        f.write(_json_line(_pending("permB", "sid-B", agent="tg")))
    r.poll_once()
    cq2 = {"id": "c2", "from": {"id": WHITELIST_UID},
           "data": ar.build_callback_data("permB", "always")}
    r.handle_callback(cq2)
    import json
    assert json.load(open(os.path.join(fch.resp_dir, "permB.json")))["decision"] == "allow"
    assert os.path.exists(os.path.join(str(fch.state_dir), "always_tg"))


def _json_line(entry):
    import json
    return json.dumps(entry, ensure_ascii=False) + "\n"
