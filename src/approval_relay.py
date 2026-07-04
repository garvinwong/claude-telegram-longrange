#!/usr/bin/env python3
# approval_relay.py — tg-longrange 审批中继（agents-island over Telegram）
#
# 设计要点：
#
# 职责：轮询agents-island桥 GET /api/state 的 pending 审批条目，只把「台账活跃会话」的条目
#   转成 Telegram inline keyboard 推给 Owner；Owner 点击 → POST /api/decision 回注会话。
#   桥不可达 → 降级直读 queue.jsonl / 直写 responses/<id>.json（格式与岛完全一致）。
#
# 设计红线：
#   · 形态=daemon 内线程（单 systemd 单元；崩溃连坐由 Restart+watchdog 兜底）。
#   · 过滤主条件=台账 active_session_ids()（本机终端开发会话不在台账 → 绝不推手机，S1/C3）；
#     agent_source=='tg' 仅作辅助信号，不单独放行。
#   · from.id 双校验：callback_query.from.id 必 ∈ 白名单，否则静默 answerCallbackQuery（S1）。
#   · callback_data 只含不可猜 perm_id + 动作枚举，无任何可执行语义；长度 < 64 字节（TG 硬限，S4）。
#   · 防幽灵批准（S4）：只对「当前仍 pending 且我方在途」的 perm_id 回写决策；
#     条目已从 pending 消失（岛先批）→ 编辑为「已在本机处理」，决不对不存在的 id 写响应文件。

import json
import os
import threading
import time

# callback_data 前缀——短前缀省字节，避开 TG 64 字节上限（ccgram 实践教训，oss-radar 已录）
_CB_PREFIX = "tglr"
_ACTIONS = ("allow", "deny", "always")

# 桥决策枚举（POST /api/decision 接受 allow|deny|always；hook 只认 allow|deny，
# 桥把 always 翻成 allow 响应 + 写 always 标志——降级路径同此语义）
_BRIDGE_URL_DEFAULT = "http://127.0.0.1:5599"


def build_callback_data(perm_id, action):
    # 结构：tglr:<perm_id>:<action>。perm_id 为 sha256前12 + '_' + 秒级时间戳 ≈ 23 字节，
    # 全长 ≈ 36 字节，稳在 64 以内（构造处即断言，防未来 perm_id 变长踩坑）
    data = f"{_CB_PREFIX}:{perm_id}:{action}"
    if len(data.encode("utf-8")) >= 64:
        # 结构性防呆：宁可拒发也不发一个会被 TG 截断的 callback_data
        raise ValueError(f"callback_data 超 64 字节: {len(data)}")
    return data


def parse_callback_data(data):
    # 返回 (perm_id, action) 或 None（非本中继的 callback / 格式非法一律拒解析）
    if not data or not data.startswith(_CB_PREFIX + ":"):
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[2] not in _ACTIONS:
        return None
    return parts[1], parts[2]


class BridgeClient:
    """桥 HTTP 客户端（GET /api/state、POST /api/decision）。requests 延迟导入，
    便于测试完全注入替身、不依赖真实桥。"""

    def __init__(self, base_url=_BRIDGE_URL_DEFAULT, timeout=3.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def get_state(self):
        # 返回 dict 或 None（桥不可达）。None 是降级信号，绝不静默当空 pending
        import requests
        try:
            r = requests.get(f"{self._base}/api/state", timeout=self._timeout)
            return r.json()
        except Exception:   # noqa: BLE001 — 任何异常都视为桥不可达 → 降级
            return None

    def post_decision(self, perm_id, decision, reason=""):
        # 返回 True(成功) / False(桥拒绝，如 410 已过期) / None(桥不可达 → 降级)
        import requests
        payload = {"id": perm_id, "decision": decision}
        if reason:
            payload["reason"] = reason
        try:
            r = requests.post(f"{self._base}/api/decision", json=payload,
                              timeout=self._timeout)
            body = r.json() if r.content else {}
            return bool(body.get("ok"))
        except Exception:   # noqa: BLE001
            return None


class FileChannel:
    """降级通道：桥不可达时直读队列、直写响应文件（格式与岛 write_response 完全一致）。

    约束：响应文件用「临时文件 + os.replace」原子落盘——hook 以「文件存在」为就绪信号
    轮询，非原子写会暴露空文件窗口，hook 读到半截 JSON 会兜底 allow（把 deny 反转）。
    """

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.queue_file = os.path.join(state_dir, "queue.jsonl")
        self.resp_dir = os.path.join(state_dir, "responses")

    def read_pending(self):
        # 降级读 pending：队列内出现、但尚无响应文件、且未过期的条目视为待审批。
        # 这是近似（无桥的 TTL/always 逻辑），仅作桥宕期间的应急，桥恢复即回主路径。
        if not os.path.exists(self.queue_file):
            return []
        entries = {}
        try:
            with open(self.queue_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue   # 坏行跳过
                    eid = e.get("id")
                    if eid:
                        entries[eid] = e   # 同 id 后出现的覆盖前者
        except OSError:
            return []
        pending = []
        for eid, e in entries.items():
            if e.get("type") == "notify":
                continue
            if os.path.exists(os.path.join(self.resp_dir, f"{eid}.json")):
                continue   # 已有响应 = 已处理
            pending.append(e)
        return pending

    def has_response(self, perm_id):
        return os.path.exists(os.path.join(self.resp_dir, f"{perm_id}.json"))

    def write_decision(self, perm_id, decision, reason="", agent_source="claude"):
        # 防幽灵批准：调用前必已确认无既存响应；此处再查一次，绝不覆盖已落决策
        os.makedirs(self.resp_dir, exist_ok=True)
        if self.has_response(perm_id):
            return False
        # always → 写 allow 响应 + always 标志（镜像桥 write_always_flag 语义）
        eff = "allow" if decision == "always" else decision
        payload = {"decision": eff}
        if reason:
            payload["reason"] = reason
        self._atomic_write(os.path.join(self.resp_dir, f"{perm_id}.json"), payload)
        if decision == "always":
            self._write_always_flag(agent_source)
        return True

    def _write_always_flag(self, agent_source):
        agent = (agent_source or "claude").lower()
        # 已知三家（claude/codex/gemini）标志名有专门映射，其余按 always_<agent> 公式；
        # 降级路径保守只处理公式路径（与桥 always_flag_path 的 fallback 分支一致）
        flag = os.path.join(self.state_dir, f"always_{agent}")
        try:
            self._atomic_write(flag, {"agent": agent, "ts": None})
        except OSError:
            pass

    @staticmethod
    def _atomic_write(path, payload):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)


class ApprovalRelay:
    """审批中继线程。构造注入 api/store/cfg + bridge/file_channel（测试可全替身）。"""

    def __init__(self, api, store, cfg, bridge=None, file_channel=None,
                 poll_interval=2.0, now_fn=time.monotonic):
        self.api = api
        self.store = store
        self.cfg = cfg
        self.bridge = bridge if bridge is not None else BridgeClient()
        state_dir = os.environ.get("ISLAND_STATE_DIR",
                                   os.path.expanduser("~/.agents-island"))
        self.files = file_channel if file_channel is not None else FileChannel(state_dir)
        self.poll_interval = poll_interval
        self.now_fn = now_fn

        self._lock = threading.Lock()
        # perm_id -> {message_id, entry, answered:bool}
        self._inflight = {}
        self._stop = threading.Event()
        self._thread = None

    # ── 线程生命周期 ─────────────────────────────────────────────────────
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def shutdown(self, join_timeout=3.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:   # noqa: BLE001 — 单轮异常不得杀死中继线程
                pass
            self._stop.wait(self.poll_interval)

    # ── 一轮轮询：拉 pending → 过滤 → 推送 → 先应者编辑 ───────────────────
    def poll_once(self):
        state = self.bridge.get_state()
        if state is not None:
            pending = state.get("pending") or []
            degraded = False
        else:
            pending = self.files.read_pending()   # 降级
            degraded = True

        active = self.store.active_session_ids()
        # 过滤主条件：会话必须在台账活跃集合内（本机终端开发会话被此闸挡在门外）
        relevant = [e for e in pending
                    if e.get("session_id") in active]
        relevant_ids = {e.get("id") for e in relevant}

        # 新出现的 pending → 推 inline keyboard
        for entry in relevant:
            eid = entry.get("id")
            if not eid:
                continue
            with self._lock:
                if eid in self._inflight:
                    continue
            self._push_card(entry, degraded)

        # 在途但已从 pending 消失（岛先批/超时）且我方未答 → 编辑为「已在本机处理」
        with self._lock:
            vanished = [eid for eid, rec in self._inflight.items()
                        if not rec["answered"] and eid not in relevant_ids]
        for eid in vanished:
            self._mark_handled_elsewhere(eid)

    def _push_card(self, entry, degraded):
        text = self._render_card(entry, degraded)
        keyboard = self._keyboard(entry.get("id"))
        chat_id = getattr(self.cfg, "CHAT_ID", None)
        mid = self.api.send_message(chat_id, text, reply_markup=keyboard)
        with self._lock:
            self._inflight[entry.get("id")] = {
                "message_id": mid, "entry": entry, "answered": False,
                "chat_id": chat_id}

    def _mark_handled_elsewhere(self, eid):
        with self._lock:
            rec = self._inflight.get(eid)
            if rec is None or rec["answered"]:
                return
            rec["answered"] = True
            mid, chat_id = rec["message_id"], rec["chat_id"]
        if mid is not None:
            self.api.edit_message(chat_id, mid, "☑️ 已在本机agents-island处理")

    # ── callback_query 处理（Owner 点按钮）───────────────────────────────
    def handle_callback(self, cq):
        """返回 True 表示本中继消化了该 callback；False 表示非本中继的 callback。"""
        cq_id = cq.get("id")
        frm = cq.get("from") or {}
        uid = frm.get("id")
        # S1：from.id 双校验第一闸——非白名单静默丢弃（answerCallbackQuery 不报错即可）
        if uid not in self.cfg.ALLOWED_USER_IDS:
            self.api.answer_callback_query(cq_id, text=None)
            return True
        parsed = parse_callback_data(cq.get("data"))
        if parsed is None:
            return False   # 不是本中继的按钮（可能是别的功能），交回上层
        perm_id, action = parsed

        with self._lock:
            rec = self._inflight.get(perm_id)
        # 防幽灵批准/重放（S4）：不在途 或 已答 → 一律回「已过期/已处理」，绝不写响应
        if rec is None or rec["answered"]:
            self.api.answer_callback_query(cq_id, text="该审批已过期或已处理")
            return True

        ok = self._commit_decision(perm_id, action, rec["entry"])
        with self._lock:
            rec["answered"] = True
            mid, chat_id, entry = rec["message_id"], rec["chat_id"], rec["entry"]

        label = {"allow": "✅ 已批准", "deny": "❌ 已拒绝",
                 "always": "⚡ 本任务后续全批"}[action]
        if not ok:
            label = "⚠️ 处理失败或已被处理"
        self.api.answer_callback_query(cq_id, text=label)
        # 改写原审批卡为结果态：editMessageText 不带 reply_markup → 撤掉按钮（防重复点），
        # 保留工具上下文让记录可读（不是光秃秃一句"已批准"）
        if mid is not None:
            tool = entry.get("tool_name", "?")
            self.api.edit_message(chat_id, mid, f"{label} · {tool}")
        return True

    def _commit_decision(self, perm_id, action, entry):
        # 优先经桥 POST /api/decision；桥不可达（None）→ 降级直写响应文件
        reason = "User denied via Telegram" if action == "deny" else ""
        result = self.bridge.post_decision(perm_id, action, reason)
        if result is True:
            return True
        if result is False:
            return False   # 桥明确拒绝（410 已过期）——不再降级重写，避免幽灵批准
        # result is None：桥不可达 → 降级
        return self.files.write_decision(
            perm_id, action, reason,
            agent_source=entry.get("agent_source", "claude"))

    # ── 渲染 ─────────────────────────────────────────────────────────────
    def _keyboard(self, perm_id):
        return {"inline_keyboard": [[
            {"text": "✅ 批准", "callback_data": build_callback_data(perm_id, "allow")},
            {"text": "❌ 拒绝", "callback_data": build_callback_data(perm_id, "deny")},
            {"text": "⚡ 全批", "callback_data": build_callback_data(perm_id, "always")},
        ]]}

    @staticmethod
    def _render_card(entry, degraded):
        tool = entry.get("tool_name", "?")
        tinput = entry.get("tool_input") or {}
        # 命令摘要：Bash 展示 command，其余展示紧凑 JSON，均截断防盲批时刷屏（S2③）
        if tool == "Bash":
            summary = str(tinput.get("command", ""))[:300]
        else:
            summary = json.dumps(tinput, ensure_ascii=False)[:300]
        title = entry.get("title") or entry.get("session_slug") or ""
        head = "🔐 工具审批请求" + ("（桥降级模式）" if degraded else "")
        return f"{head}\n任务: {title}\n工具: {tool}\n{summary}"
