#!/usr/bin/env python3
# test_approval_relay.py — 审批中继单测（pytest）
#
# 约束（红线）：桥客户端 + TG API 全替身，绝不联网、绝不碰 5599 生产桥；
#   降级路径用 tmp_path 造 queue/responses，绝不写真实 ~/.agents-island/。DB 用 tmp_path。
#
# 运行：python3 -m pytest apps/tg-longrange/tests/test_approval_relay.py -v

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
    # config 白名单来自环境变量，测试环境为空——注入占位白名单，等价线上鉴权
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
    r._degrade_after = 1        # 本用例只验降级路径本身，跳过抖动缓冲
    # 造队列文件：一条属活跃会话的「新鲜」待审批（带当前时间戳，避开 TTL 过滤）
    permd = _fresh_id("permD")
    with open(fch.queue_file, "w", encoding="utf-8") as f:
        f.write(_json_line(_pending(permd, "sid-A")))
    r.poll_once()
    assert len(api.sent) == 1   # 降级也能推卡（卡上标注降级模式）
    assert "降级" in api.sent[0]["text"]
    # allow → 直写响应文件（桥不可达，post_decision 返回 None）
    cq = {"id": "c", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data(permd, "allow")}
    r.handle_callback(cq)
    resp_path = os.path.join(fch.resp_dir, f"{permd}.json")
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
    assert any("已在电脑端" in e["text"] for e in api.edited)
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


# ── 点击后审批卡改写为结果态，保留工具上下文、撤掉按钮 ────────────────
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
    r._degrade_after = 1
    _active_task(store, "sid-B")
    permb = _fresh_id("permB")
    with open(fch.queue_file, "w", encoding="utf-8") as f:
        f.write(_json_line(_pending(permb, "sid-B", agent="tg")))
    r.poll_once()
    cq2 = {"id": "c2", "from": {"id": WHITELIST_UID},
           "data": ar.build_callback_data(permb, "always")}
    r.handle_callback(cq2)
    import json
    assert json.load(open(os.path.join(fch.resp_dir, f"{permb}.json")))["decision"] == "allow"
    assert os.path.exists(os.path.join(str(fch.state_dir), "always_tg"))


# ── ⑫ 桥单次抖动（miss < 阈值）→ 本轮不动任何卡（不回退文件、不刷屏）─────────
def test_transient_bridge_miss_does_not_degrade(relay):
    r, api, bridge, store, fch = relay
    _active_task(store, "sid-A")
    bridge.unreachable = True
    r._degrade_after = 3
    # 队列里有一条历史条目；抖动期绝不能被文件降级路径捞出来推送
    with open(fch.queue_file, "w", encoding="utf-8") as f:
        f.write(_json_line(_pending(_fresh_id("permT"), "sid-A")))
    r.poll_once()   # miss 1
    r.poll_once()   # miss 2（仍 < 3）
    assert api.sent == []   # 抖动期一条都不推


# ── ⑬ 桥持续不可达（miss ≥ 阈值）→ 才切文件降级 ───────────────────────────
def test_sustained_bridge_outage_degrades(relay):
    r, api, bridge, store, fch = relay
    _active_task(store, "sid-A")
    bridge.unreachable = True
    r._degrade_after = 3
    with open(fch.queue_file, "w", encoding="utf-8") as f:
        f.write(_json_line(_pending(_fresh_id("permS"), "sid-A")))
    r.poll_once(); r.poll_once(); r.poll_once()   # 第 3 次跨过阈值
    assert len(api.sent) == 1
    assert "降级" in api.sent[0]["text"]
    # 桥恢复 → miss 计数归零（不影响后续主路径）
    bridge.unreachable = False
    bridge.state = {"pending": []}
    r.poll_once()
    assert r._miss == 0


# ── ⑭ 降级绝不回涌历史积压：老时间戳条目（hook 早已 defer）不推送 ──────────
def test_degraded_ttl_suppresses_stale_backlog(relay):
    r, api, bridge, store, fch = relay
    _active_task(store, "sid-A")
    bridge.unreachable = True
    r._degrade_after = 1
    now = 1_783_000_000
    # 一条老掉牙（超 max_age，hook 早 defer）+ 一条新鲜（<max_age）
    stale = f"aaaa_{now - 5000}"       # 5000s 前 → 远超 660s 窗口
    fresh = f"bbbb_{now - 10}"         # 10s 前 → 仍在窗口内
    with open(fch.queue_file, "w", encoding="utf-8") as f:
        f.write(_json_line(_pending(stale, "sid-A")))
        f.write(_json_line(_pending(fresh, "sid-A")))
    # 注入固定时钟供 TTL 比较（read_pending 用 wall-clock）
    fch.now_epoch_fn = lambda: float(now)
    r.poll_once()
    pushed_ids = [s["text"] for s in api.sent]
    assert len(api.sent) == 1              # 只推新鲜那条
    assert any(fresh in s["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
               for s in api.sent)


# ── ⑮ read_pending 直测：TTL 边界（等于/略超窗口）──────────────────────────
def test_read_pending_ttl_boundary(relay):
    _r, _api, _bridge, _store, fch = relay
    now = 2_000_000_000.0
    ok = f"cccc_{int(now) - 100}"          # 100s 前，窗口内
    old = f"dddd_{int(now) - 5000}"        # 5000s 前，窗口外
    noant = "eeee_notanumber"              # 无法解析时间戳 → 保守放行（不参与 TTL）
    with open(fch.queue_file, "w", encoding="utf-8") as f:
        for eid in (ok, old, noant):
            f.write(_json_line(_pending(eid, "sid-A")))
    got = {e["id"] for e in fch.read_pending(now_epoch=now)}
    assert ok in got and noant in got and old not in got


# ── AskUserQuestion（选择题）适配 ─────────────────────────────────────────
def _ask_pending(eid, sid, question="合并方式？", header="合并方式",
                 options=None, multi=False):
    if options is None:
        options = [{"label": "只挑 3 个", "description": "cherry-pick 到 main"},
                   {"label": "整条分支全合", "description": "把 20+ 无关 commit 也带上"}]
    q = {"question": question, "header": header, "options": options}
    if multi:
        q["multiSelect"] = True
    return {"id": eid, "session_id": sid, "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [q]}, "agent_source": "tg",
            "title": "crowdsec 优化"}


# ── ⑯ 选择题 → 题干+逐项按钮（一选项一按钮 + 末行终端作答）───────────────────
def test_askuserquestion_renders_option_buttons(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_ask_pending("permQ", "sid-A")]}
    r.poll_once()
    assert len(api.sent) == 1
    card = api.sent[0]
    assert "选择题" in card["text"]
    assert "合并方式" in card["text"] and "只挑 3 个" in card["text"]
    rows = card["reply_markup"]["inline_keyboard"]
    # 两个选项 → 两行选项按钮 + 一行"终端作答"
    assert len(rows) == 3
    assert rows[0][0]["callback_data"] == ar.build_callback_data("permQ", "opt0")
    assert rows[1][0]["callback_data"] == ar.build_callback_data("permQ", "opt1")
    assert rows[2][0]["callback_data"] == ar.build_callback_data("permQ", "allow")


# ── ⑰ 点选项 → deny+reason 携带所选，回传模型；卡塌缩为「已作答」──────────────
def test_askuserquestion_answer_commits_choice(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_ask_pending("permQ", "sid-A")]}
    r.poll_once()
    cq = {"id": "cq", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data("permQ", "opt1")}
    assert r.handle_callback(cq) is True
    posted = bridge.posted[-1]
    assert posted["decision"] == "deny"           # 协议：作答走 deny+reason
    assert "整条分支全合" in posted["reason"]      # reason 携带所选项 label
    assert "作答" in api.answered[-1]["text"]
    assert any("已作答" in e["text"] and "整条分支全合" in e["text"]
               for e in api.edited)


# ── ⑱ 多选/多问题 → 回落普通三档审批（不作选择题渲染）─────────────────────────
def test_askuserquestion_multiselect_falls_back(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_ask_pending("permM", "sid-A", multi=True)]}
    r.poll_once()
    rows = api.sent[0]["reply_markup"]["inline_keyboard"]
    assert len(rows) == 1 and len(rows[0]) == 3   # 单行三档：批准/拒绝/全批
    assert [b["text"] for b in rows[0]] == ["✅ 批准", "❌ 拒绝", "⚡ 全批"]


# ── ⑲ 越界选项 → 提示失效、不落桥（防伪造 opt 序号）──────────────────────────
def test_askuserquestion_out_of_range_option(relay):
    r, api, bridge, store, _ = relay
    _active_task(store, "sid-A")
    bridge.state = {"pending": [_ask_pending("permQ", "sid-A")]}
    r.poll_once()
    cq = {"id": "cq", "from": {"id": WHITELIST_UID},
          "data": ar.build_callback_data("permQ", "opt9")}  # 只有 2 个选项
    assert r.handle_callback(cq) is True
    assert bridge.posted == []                    # 越界不落桥
    assert "失效" in api.answered[-1]["text"]


# ── ⑳ opt<N> callback_data 编解码往返 ─────────────────────────────────────
def test_option_callback_roundtrip():
    data = ar.build_callback_data("abc_123", "opt0")
    assert ar.parse_callback_data(data) == ("abc_123", "opt0")
    assert ar.parse_callback_data("tglr:abc_123:opt12") == ("abc_123", "opt12")
    assert ar.parse_callback_data("tglr:abc_123:optX") is None  # 非数字序号拒解析


def _fresh_id(prefix):
    # 造一个「当前时间戳」的 perm_id（<prefix>_<近未来 unix_ts>），避开降级 TTL 过滤
    return f"{prefix}_2000000000"


def _json_line(entry):
    import json
    return json.dumps(entry, ensure_ascii=False) + "\n"
