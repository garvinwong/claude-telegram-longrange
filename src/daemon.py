#!/usr/bin/env python3
# daemon.py — tg-longrange TG 接口层（网络层 TgApi + 路由层 Daemon 分离）
#
# 设计要点：
# 结构铁律（可测性）：网络层（TgApi，唯一碰 requests 的地方）与路由层（Daemon）分离，
#   Daemon 构造时注入 api/store/cfg，测试可全 mock，绝不真实联网、绝不起真 claude。
#
# 安全红线（S2）：全链路无 shell=True、无 f-string 拼命令；
#   用户文本只作 subprocess argv 的单个元素或 requests json 载荷，绝不进 shell。
#
# 顶层绝不 import progress——worker 内 lazy import，
#   模块不存在时降级为仅日志，保证本批可独立测试。

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import uuid

import requests

# 无包结构：把本目录挂 sys.path，供 config/tasks/runner 同级导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config   # noqa: E402
import runner   # noqa: E402
import tasks     # noqa: E402

log = logging.getLogger("tg-longrange")

# 状态 → emoji（/tasks 列表展示用），越界状态回退 ❔
_STATUS_EMOJI = {
    tasks.STATUS_QUEUED: "⏳",
    tasks.STATUS_RUNNING: "🏃",
    tasks.STATUS_WAITING_APPROVAL: "⏸️",
    tasks.STATUS_DONE: "✅",
    tasks.STATUS_FAILED: "❌",
    tasks.STATUS_CANCELLED: "🚫",
    tasks.STATUS_INTERRUPTED: "⚠️",
}

# 停机哨兵：放入队列让 dispatcher 越过 acquire/get 后退出
_SHUTDOWN = object()

# 面板分页：每页按钮数 + 单次扫描上限（避免会话/任务过多时构造超大键盘）
_PAGE_SIZE = 8
_SESSION_SCAN_CAP = 40

# 「/」自动补全菜单：daemon 启动时 setMyCommands 覆盖注册（清 v1/上游 /status 等残留）。
# 新增命令时在此登记即自动进菜单——单一真源，防再漏。description ≤256 字符。
BOT_COMMANDS = [
    {"command": "new",      "description": "起长程任务（-m opus|sonnet|haiku 可选模型）"},
    {"command": "resume",   "description": "选一个任务继续（弹面板，免打ID）"},
    {"command": "sessions", "description": "接管电脑上的会话（弹面板，免打ID）"},
    {"command": "tasks",    "description": "查看任务列表"},
    {"command": "say",      "description": "给某任务续话：/say 编号 文字"},
    {"command": "model",    "description": "设默认模型（弹面板；/new 不带 -m 时用它）"},
    {"command": "rename",   "description": "重命名任务：/rename 编号 新名称"},
    {"command": "cancel",   "description": "终止某任务：/cancel 编号"},
    {"command": "attach",   "description": "按短ID接管会话：/attach 短ID"},
    {"command": "help",     "description": "查看用法"},
]


# ── 短问答路径（过渡期保留 v1 语义）─────────────────────────────────────────
def ask_claude(text, cwd=None, timeout=120):
    """无命令前缀短文本 / 图片视觉问答走这里，照抄 v1 语义。

    约束：仍用 claude -p --dangerously-skip-permissions（v1 行为不变，过渡期）。
    相关设计：短问答收编进长程 runner（去 skip-permissions、走审批门）是后续计划，
    本批只做等价移植，不改行为。argv 列表传参、shell=False（S2）。
    """
    try:
        result = subprocess.run(
            [runner.CLAUDE_BIN, "-p", "--dangerously-skip-permissions", text],
            capture_output=True, text=True,
            cwd=cwd or config.WORKDIR, timeout=timeout,
        )
        reply = (result.stdout or "").strip()
        if not reply and result.stderr:
            reply = f"[错误] {result.stderr.strip()[:500]}"
        return reply or "（无回复）"
    except subprocess.TimeoutExpired:
        return "处理超时，请稍后再试。"
    except Exception as e:   # noqa: BLE001
        return f"[调用失败] {config.redact(str(e))}"


# ── 网络层：唯一与 requests / Telegram HTTP API 交互的地方 ───────────────────
class TgApi:
    """封装 Telegram Bot API 调用。所有异常经 config.redact 后记日志（S3②）。"""

    def __init__(self, token):
        # 约束：token 不入日志、不入异常消息；base 只在本对象内部使用
        self._base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"

    def _log_exc(self, where, exc):
        # requests 异常 repr 会带完整 URL（含 token）——必须脱敏
        log.warning("%s 失败: %s", where, config.redact(repr(exc)))

    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML"):
        """发送消息，返回 message_id（成功）或 None（失败）。"""
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            r = requests.post(f"{self._base}/sendMessage", json=payload, timeout=10)
            return (r.json().get("result") or {}).get("message_id")
        except Exception as e:   # noqa: BLE001
            self._log_exc("sendMessage", e)
            return None

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        """编辑消息。返回 {'ok':bool, 'retry_after':float|None}。

        约束：429 时从响应 parameters.retry_after 取退避秒数返回，
        本层不重试——退避策略交由上层确定性执行（R4）。
        """
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
                   "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            r = requests.post(f"{self._base}/editMessageText", json=payload, timeout=10)
            body = r.json()
            if body.get("ok"):
                return {"ok": True, "retry_after": None}
            retry_after = (body.get("parameters") or {}).get("retry_after")
            return {"ok": False,
                    "retry_after": float(retry_after) if retry_after is not None else None}
        except Exception as e:   # noqa: BLE001
            self._log_exc("editMessageText", e)
            return {"ok": False, "retry_after": None}

    def send_chat_action(self, chat_id, action="typing"):
        try:
            requests.post(f"{self._base}/sendChatAction",
                          json={"chat_id": chat_id, "action": action}, timeout=5)
        except Exception as e:   # noqa: BLE001
            self._log_exc("sendChatAction", e)

    def get_updates(self, offset, timeout=25):
        """长轮询。返回 updates 列表；网络异常返回 None 由上层重试。"""
        try:
            r = requests.get(f"{self._base}/getUpdates",
                             params={"offset": offset, "timeout": timeout},
                             timeout=timeout + 5)
            return r.json().get("result", [])
        except Exception as e:   # noqa: BLE001
            self._log_exc("getUpdates", e)
            return None

    def get_file(self, file_id):
        """取 file_path（下载图片第一步）。返回路径字符串或 None。"""
        try:
            r = requests.get(f"{self._base}/getFile",
                             params={"file_id": file_id}, timeout=10)
            return (r.json().get("result") or {}).get("file_path")
        except Exception as e:   # noqa: BLE001
            self._log_exc("getFile", e)
            return None

    def download(self, file_path, save_path):
        """下载图片到本地。成功 True。"""
        try:
            data = requests.get(f"{self._file_base}/{file_path}", timeout=30).content
            with open(save_path, "wb") as f:
                f.write(data)
            return True
        except Exception as e:   # noqa: BLE001
            self._log_exc("download", e)
            return False

    def answer_callback_query(self, callback_query_id, text=None):
        payload = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        try:
            requests.post(f"{self._base}/answerCallbackQuery", json=payload, timeout=5)
        except Exception as e:   # noqa: BLE001
            self._log_exc("answerCallbackQuery", e)

    def delete_webhook(self):
        # 曾有 webhook 残留致长轮询收不到消息的实案（R4）——启动前必删
        try:
            requests.post(f"{self._base}/deleteWebhook", timeout=10)
        except Exception as e:   # noqa: BLE001
            self._log_exc("deleteWebhook", e)

    def set_message_reaction(self, chat_id, message_id, emoji="👀"):
        """给用户消息加 emoji 反应（比 typing 更持久的"我在处理"信号）。失败不致命。"""
        try:
            requests.post(f"{self._base}/setMessageReaction",
                          json={"chat_id": chat_id, "message_id": message_id,
                                "reaction": [{"type": "emoji", "emoji": emoji}]},
                          timeout=10)
        except Exception as e:   # noqa: BLE001
            self._log_exc("setMessageReaction", e)

    def set_my_commands(self, commands):
        """注册「/」自动补全菜单（Telegram 服务端持久保存）。
        commands: [{'command': 'new', 'description': '...'}, ...]。
        覆盖式：传入即为完整菜单，会清掉上一版（如 v1/上游残留的 /status 等）。"""
        try:
            requests.post(f"{self._base}/setMyCommands",
                          json={"commands": commands}, timeout=10)
        except Exception as e:   # noqa: BLE001
            self._log_exc("setMyCommands", e)


# ── 路由层 ──────────────────────────────────────────────────────────────────
class Daemon:
    """TG 更新路由 + 长任务执行调度。

    构造注入 api/store/cfg 便于测试全 mock。执行调度用
    Semaphore(MAX_CONCURRENCY) + FIFO queue.Queue：dispatcher 先 acquire 再 get，
    满并发时后续任务留在队列（状态仍 queued），一个 worker 结束释放 slot 才出队。
    """

    def __init__(self, api, store, cfg):
        self.api = api
        self.store = store
        self.cfg = cfg
        self._queue = queue.Queue()
        self._sem = threading.Semaphore(cfg.MAX_CONCURRENCY)
        self._stop = False
        self._dispatcher = None
        self._workers_started = False
        self._workers = []               # 在途 worker 线程，shutdown 时 join，防关库竞态
        self._workers_lock = threading.Lock()
        # 默认模型（/model 设置，/new 不带 -m 时用它）；持久化到文件，跨重启保留
        self._default_model_file = os.path.join(
            os.path.dirname(cfg.OFFSET_FILE), "default_model")
        self._default_model = self._load_default_model()
        # 每个 chat 的「当前会话」指针（默认连续对话：普通文本接着它聊）。持久化跨重启。
        self._current_file = os.path.join(
            os.path.dirname(cfg.OFFSET_FILE), "current_sessions.json")
        self._current = self._load_current()   # {chat_id(str): task_id(int)}
        self._current_lock = threading.Lock()
        # 审批中继：daemon 内线程，构造即备好，run() 时 start()。
        # 延迟导入避免纯路由测试硬依赖；缺失时降级为无中继（callback 只静默 answer）。
        self._relay = None
        try:
            import approval_relay
            self._relay = approval_relay.ApprovalRelay(api, store, cfg)
        except Exception as e:   # noqa: BLE001
            log.warning("审批中继初始化失败，callback 将仅静默应答: %s",
                        config.redact(repr(e)))
        # 构造即启动调度线程：空队列时阻塞在 get，不影响纯路由测试
        self.start_workers()

    # ── 执行调度 ────────────────────────────────────────────────────────
    def start_workers(self):
        if self._workers_started:
            return
        self._workers_started = True
        self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatcher.start()

    def _dispatch_loop(self):
        # 先 acquire slot 再 get item：满并发时排队任务不出队、状态保持 queued（R5）
        while True:
            self._sem.acquire()
            item = self._queue.get()
            if item is _SHUTDOWN:
                self._sem.release()
                break
            t = threading.Thread(target=self._run_task, args=(item,), daemon=True)
            with self._workers_lock:
                # 顺带清理已结束线程，避免长跑积累
                self._workers = [w for w in self._workers if w.is_alive()]
                self._workers.append(t)
            t.start()

    def shutdown(self, join_timeout=3.0):
        self._stop = True
        if self._relay is not None:
            self._relay.shutdown(join_timeout=join_timeout)
        # 释放一个 slot 确保 dispatcher 能越过 acquire 拿到哨兵；再投哨兵
        self._sem.release()
        self._queue.put(_SHUTDOWN)
        # 先 join 调度线程：它退出后不再新建 worker，此时 _workers 才是完整集合
        #（否则哨兵前若还排着真实 item，dispatcher 会在快照之后再 spawn worker，漏 join）
        if self._dispatcher is not None:
            self._dispatcher.join(timeout=join_timeout)
        # join 在途 worker：避免调用方随后关库时 worker 仍在写库（关库竞态）
        with self._workers_lock:
            workers = list(self._workers)
        for w in workers:
            w.join(timeout=join_timeout)

    def _enqueue(self, item):
        self._queue.put(item)

    def _run_task(self, item):
        """worker：台账置 running → runner.start/resume（阻塞）→ 落终态。finally 释放 slot。"""
        task_id = item["task_id"]
        try:
            row = self.store.get(task_id)
            # queued 期间被 /cancel → 出队即弃（不执行）
            if not row or row["status"] == tasks.STATUS_CANCELLED:
                return
            self.store.update_status(task_id, tasks.STATUS_RUNNING, last_event="已起进程")

            chat_id = item["chat_id"]
            title = item.get("title") or ""
            model = item.get("model")

            # 进度卡：由 progress.py 提供，本批 lazy import，缺失降级为仅日志。
            # 契约：ProgressCard(send_fn, edit_fn, chat_id, task_id, title, session_id,
            #   model=None, throttle_s=20)；.message_id 属性 = 进度卡 message_id；
            #   handle_event(event) / finish(status, note=None)。
            card = None
            try:
                import progress   # noqa: PLC0415 — 顶层禁 import，只在此 lazy import
                card = progress.ProgressCard(
                    self.api.send_message, self.api.edit_message,
                    chat_id, task_id, title, item.get("session_id"),
                    model=model, throttle_s=self.cfg.PROGRESS_THROTTLE)
                if getattr(card, "message_id", None):
                    self.store.update_fields(task_id, progress_msg_id=card.message_id)
            except ImportError:
                card = None
            except Exception as e:   # noqa: BLE001 — 进度卡不得拖垮任务执行
                log.warning("进度卡初始化失败: %s", config.redact(repr(e)))
                card = None

            def on_event(event):
                # runner 标准事件落台账；进度卡消费同一事件（存在时）
                etype = event.get("type")
                if etype == "spawn":
                    self.store.update_fields(
                        task_id, pid=event.get("pid"), pgid=event.get("pgid"),
                        session_id=event.get("session_id"))
                elif etype in ("result", "error"):
                    # /cancel 已落 cancelled 的任务，迟到的 result/error 不得覆盖
                    #（killpg 后偶发流尾竞态——取消语义优先，C4 同源护栏）
                    cur = self.store.get(task_id) or {}
                    if cur.get("status") != tasks.STATUS_CANCELLED:
                        if etype == "result":
                            status = (tasks.STATUS_DONE if event.get("ok")
                                      else tasks.STATUS_FAILED)
                            self.store.update_status(
                                task_id, status,
                                last_event=(event.get("result_text") or "")[:200])
                        else:
                            self.store.update_status(
                                task_id, tasks.STATUS_FAILED,
                                last_event=(event.get("message") or "error")[:200])
                if card is not None:
                    try:
                        card.handle_event(event)
                    except Exception as e:   # noqa: BLE001
                        log.warning("进度卡事件处理失败: %s", config.redact(repr(e)))

            try:
                if item["kind"] == "resume":
                    runner.resume(item["session_id"], item["prompt"], model=model,
                                  on_event=on_event, cwd=self.cfg.WORKDIR,
                                  timeout=self.cfg.TASK_TIMEOUT)
                else:
                    runner.start(item["prompt"], model=model, on_event=on_event,
                                 session_id=item.get("session_id"),
                                 cwd=self.cfg.WORKDIR, timeout=self.cfg.TASK_TIMEOUT)
            except Exception as e:   # noqa: BLE001
                log.error("任务执行异常 task=%s: %s", task_id, config.redact(repr(e)))
                self.store.update_status(task_id, tasks.STATUS_FAILED,
                                         last_event="执行异常")

            # 终态收口：进程已结束但台账仍 running（被杀/静默崩溃无 result 事件）
            # → 标 interrupted 可接力；随后 card.finish 发终态卡+另发通知
            final = self.store.get(task_id) or {}
            if final.get("status") == tasks.STATUS_RUNNING:
                self.store.update_status(task_id, tasks.STATUS_INTERRUPTED,
                                         last_event="进程结束但无终态事件，可 /say 接力")
                final = self.store.get(task_id) or {}
            if card is not None and final.get("status") in (
                    tasks.STATUS_DONE, tasks.STATUS_FAILED,
                    tasks.STATUS_CANCELLED, tasks.STATUS_INTERRUPTED):
                try:
                    card.finish(final["status"], note=final.get("last_event"))
                except Exception as e:   # noqa: BLE001
                    log.warning("进度卡终态处理失败: %s", config.redact(repr(e)))
        finally:
            self._sem.release()

    # ── 发送辅助（超长分段，v1 textwrap 语义）───────────────────────────────
    def _send(self, chat_id, text, reply_markup=None):
        chunks = textwrap.wrap(text, self.cfg.TG_MAX, replace_whitespace=False,
                               break_long_words=False) or [text]
        mid = None
        for i, chunk in enumerate(chunks):
            # reply_markup 只挂最后一段
            rm = reply_markup if i == len(chunks) - 1 else None
            mid = self.api.send_message(chat_id, chunk, reply_markup=rm)
        return mid

    # ── 主路由入口（测试主入口）───────────────────────────────────────────
    def handle_update(self, update):
        """单条 update 的完整路由。顺序铁律见各分支注释。"""
        # 回调查询（审批按钮）：本批只建 from.id 校验骨架，relay 由审批中继接管
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        # ① 白名单（S1）：from.id 不在白名单 → 静默丢弃记日志
        frm = msg.get("from") or {}
        uid = frm.get("id")
        if uid not in self.cfg.ALLOWED_USER_IDS:
            log.info("丢弃非白名单消息 from.id=%s", uid)
            return
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":   # 群聊/频道一律丢弃（S1 chat.type 护栏）
            log.info("丢弃非私聊消息 chat.type=%s", chat.get("type"))
            return
        chat_id = str(chat.get("id"))

        # ② 图片消息 → 视觉问答路径（走短问答）
        if msg.get("photo"):
            self._handle_photo(msg, chat_id)
            return

        text = (msg.get("text") or "").strip()
        if not text:
            return

        # ③ 对进度卡的回复 → 等价 /say 该任务
        reply = msg.get("reply_to_message")
        if reply:
            task = self._task_by_progress_msg(reply.get("message_id"))
            if task:
                self._say(chat_id, task, text)
                return

        # ④ 命令路由 / 连续对话（普通文本 = 接着当前会话聊，有记忆+审批+进度）
        if text.startswith("/"):
            self._route_command(chat_id, text, msg_id=msg.get("message_id"))
        else:
            self._converse(chat_id, text, msg_id=msg.get("message_id"))

    # ── callback_query → 面板选择（sess/tsel）或审批中继（tglr）──────────────
    def _handle_callback(self, cq):
        data = cq.get("data") or ""
        # 面板选择回调（daemon 自管）：白名单闸在前（S1），再路由
        if data.startswith(("sess:", "tsel:", "mdl:", "sesspg:", "taskpg:")):
            frm = cq.get("from") or {}
            if frm.get("id") not in self.cfg.ALLOWED_USER_IDS:
                log.info("丢弃非白名单 picker callback from.id=%s", frm.get("id"))
                self.api.answer_callback_query(cq.get("id"))
                return
            chat_id = str((cq.get("message") or {}).get("chat", {}).get("id")
                          or frm.get("id"))
            mid = (cq.get("message") or {}).get("message_id")
            if data.startswith("sesspg:"):
                # 翻页：就地编辑同一条面板消息（不刷屏）
                self._send_sessions_panel(chat_id, page=self._pg(data), edit_mid=mid)
                self.api.answer_callback_query(cq.get("id"))
            elif data.startswith("taskpg:"):
                self._send_tasks_panel(chat_id, page=self._pg(data), edit_mid=mid)
                self.api.answer_callback_query(cq.get("id"))
            elif data.startswith("sess:"):
                self._cb_pick_session(chat_id, data[len("sess:"):], cq.get("id"), mid)
            elif data.startswith("tsel:"):
                self._cb_pick_task(chat_id, data[len("tsel:"):], cq.get("id"), mid)
            else:
                self._cb_pick_model(chat_id, data[len("mdl:"):], cq.get("id"), mid)
            return
        # 审批回调 → 中继（内部已做 from.id 双校验/幽灵防护）
        if self._relay is not None and self._relay.handle_callback(cq):
            return
        frm = cq.get("from") or {}
        if frm.get("id") not in self.cfg.ALLOWED_USER_IDS:
            log.info("丢弃非白名单 callback from.id=%s", frm.get("id"))
        self.api.answer_callback_query(cq.get("id"))

    @staticmethod
    def _pg(data):
        # 从 "<prefix>:<page>" 取页码，非法回 0（防伪造 callback）
        try:
            return max(0, int(data.split(":", 1)[1]))
        except (ValueError, IndexError):
            return 0

    def _collapse_panel(self, chat_id, mid, text):
        # 点选后把面板消息就地改写为结果（editMessageText 不带 reply_markup → 撤掉按钮），
        # 防误触/重复点；mid 缺失（如测试或旧消息）则跳过
        if mid is not None:
            self.api.edit_message(chat_id, mid, text)

    def _cb_pick_session(self, chat_id, full, cq_id, mid=None):
        # 面板点选会话：接管并「切为当前会话」——此后普通文本直接接着它聊
        if full not in self._all_session_stems():
            self.api.answer_callback_query(cq_id, text="会话已不存在")
            self._collapse_panel(chat_id, mid, "⚠️ 该会话已不存在。")
            return
        task_id = self._attach_session(chat_id, full)
        if task_id:
            self._collapse_panel(
                chat_id, mid, f"✅ 已切到会话 #{task_id}，直接发消息即可续话。")
        self.api.answer_callback_query(
            cq_id, text=("已切到该会话" if task_id else "该会话已有活跃任务"))

    def _cb_pick_task(self, chat_id, tid_str, cq_id, mid=None):
        # 面板点选台账任务：切为当前会话，此后普通文本直接接着它聊（不再靠回复锚点）
        if not tid_str.isdigit():
            self.api.answer_callback_query(cq_id, text="无效任务")
            return
        task = self.store.get(int(tid_str))
        if not task or not task["session_id"]:
            self.api.answer_callback_query(cq_id, text="任务不存在或无会话")
            return
        self._set_current(chat_id, task["task_id"])
        self._collapse_panel(
            chat_id, mid,
            f"✅ 已切到会话 #{task['task_id']}：{task['title'] or ''}\n直接发消息即可续话。")
        self.api.answer_callback_query(cq_id, text="已切到该会话")

    # ── 图片消息（视觉问答）────────────────────────────────────────────────
    def _handle_photo(self, msg, chat_id):
        self.api.send_chat_action(chat_id)
        photos = msg.get("photo") or []
        best = photos[-1]                       # 取最高分辨率（列表末项）
        file_path = self.api.get_file(best.get("file_id"))
        caption = (msg.get("caption") or "").strip()
        fd, img_path = tempfile.mkstemp(prefix="tg_img_", suffix=".jpg")
        os.close(fd)
        ok = bool(file_path) and self.api.download(file_path, img_path)
        if ok:
            prompt = (
                f"用户通过 Telegram 发来了一张图片，已保存在本地路径：{img_path}\n"
                f"请用 Read 工具读取该图片进行视觉分析。\n"
                f"用户附带说明：{caption if caption else '（无）'}"
            )
            reply = ask_claude(prompt, cwd=self.cfg.WORKDIR)
            try:
                os.remove(img_path)
            except OSError:
                pass
        else:
            reply = "⚠️ 图片下载失败，请重试。"
        self._send(chat_id, reply)

    # ── 短问答（无命令前缀文本）──────────────────────────────────────────────
    def _short_qa(self, chat_id, text):
        # 仅图片视觉问答仍走此一次性路径；文本已改走 _converse（连续对话）
        self.api.send_chat_action(chat_id)
        reply = ask_claude(text, cwd=self.cfg.WORKDIR)
        self._send(chat_id, config.md_to_html(reply))

    # ── 当前会话指针（连续对话）────────────────────────────────────────────
    def _load_current(self):
        try:
            with open(self._current_file, encoding="utf-8") as f:
                return {str(k): int(v) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return {}

    def _set_current(self, chat_id, task_id):
        with self._current_lock:
            self._current[str(chat_id)] = task_id
            try:
                os.makedirs(os.path.dirname(self._current_file), exist_ok=True)
                with open(self._current_file, "w", encoding="utf-8") as f:
                    json.dump(self._current, f)
            except OSError as e:
                log.warning("当前会话指针持久化失败: %s", config.redact(repr(e)))

    def _converse(self, chat_id, text, msg_id=None):
        # 默认连续对话：普通文本接着「当前会话」聊（有记忆）。无则新建；running 则请稍候。
        if msg_id is not None:
            self.api.set_message_reaction(chat_id, msg_id, "👀")
        self.api.send_chat_action(chat_id)
        tid = self._current.get(str(chat_id))
        task = self.store.get(tid) if tid else None
        if task and task["status"] == tasks.STATUS_RUNNING:
            self._send(chat_id, "⏳ 还在处理上一条，完成后再发（或 /new 开新对话）。")
            return
        if task and task["session_id"]:
            # 接着聊：resume 当前会话（安静入队，进度卡会显示处理态，不再刷"已受理"）
            self.store.update_status(task["task_id"], tasks.STATUS_QUEUED,
                                     last_event="连续对话续话")
            self._enqueue({"kind": "resume", "task_id": task["task_id"],
                           "chat_id": chat_id, "prompt": text,
                           "model": task["model"], "title": task["title"],
                           "session_id": task["session_id"]})
            return
        # 无当前会话 → 新建一个对话会话（origin=chat），此后普通文本都接着它聊
        sid = str(uuid.uuid4())
        new_id = self.store.create(session_id=sid, title=text[:60], model=self._default_model,
                                   origin="chat", status=tasks.STATUS_QUEUED, chat_id=chat_id)
        self._set_current(chat_id, new_id)
        self._enqueue({"kind": "new", "task_id": new_id, "chat_id": chat_id,
                       "prompt": text, "model": self._default_model,
                       "title": text[:60], "session_id": sid})

    # ── 命令路由 ────────────────────────────────────────────────────────────
    def _route_command(self, chat_id, text, msg_id=None):
        # 取首个 token 作命令名（不带斜杠拼接进任何 shell）
        cmd = text.split(None, 1)[0]
        if cmd == "/new":
            self._cmd_new(chat_id, text, msg_id=msg_id)
        elif cmd == "/tasks":
            self._cmd_tasks(chat_id)
        elif cmd == "/say":
            self._cmd_say(chat_id, text)
        elif cmd == "/cancel":
            self._cmd_cancel(chat_id, text)
        elif cmd == "/sessions":
            self._cmd_sessions(chat_id)
        elif cmd == "/resume":
            self._cmd_resume(chat_id)
        elif cmd == "/attach":
            self._cmd_attach(chat_id, text)
        elif cmd == "/model":
            self._cmd_model(chat_id, text)
        elif cmd == "/rename":
            self._cmd_rename(chat_id, text)
        elif cmd in ("/help", "/start"):
            self._send(chat_id, self._help_text())
        else:
            self._send(chat_id, "未知命令。发 /help 看用法。")

    @staticmethod
    def _help_text():
        return (
            "🤖 <b>长程会话（tg-longrange）用法</b>\n\n"
            "<b>直接聊</b>\n"
            "· 直接发文字（无需 /）— 接着<b>当前会话</b>连续对话，有记忆、需审批的工具会弹按钮\n"
            "· /new &lt;描述&gt; — 开一个<b>全新</b>会话（清空上下文重开）\n"
            "· /new -m opus|sonnet|haiku &lt;描述&gt; — 新会话并指定模型\n\n"
            "<b>切换/管理会话</b>（“当前会话”只有一个，切了就直接聊）\n"
            "· /resume — <b>弹面板</b>选一个任务<b>切为当前会话</b>，之后直接发消息续话\n"
            "· /sessions — <b>弹面板</b>选电脑上开的会话切为当前（免手打长 ID）\n"
            "· /tasks — 列最近任务（编号/状态/短 sid）\n"
            "· /say &lt;编号&gt; &lt;文字&gt; — 给指定任务发一句并切为当前会话\n"
            "· /attach &lt;短ID&gt; — 文本方式接管电脑会话（同 /sessions）\n"
            "· /cancel &lt;编号&gt; — 终止任务\n"
            "· /model — <b>弹面板</b>设默认模型（/new 不带 -m 时用它）\n"
            "· /rename &lt;编号&gt; &lt;新名&gt; — 重命名任务，列表更好认\n\n"
            "<b>审批</b>：危险工具会弹 ✅批准/❌拒绝/⚡全批 按钮；"
            "本机 agents-island 与手机哪边先点哪边算数。\n"
            "进度卡带 sid，回电脑可 <code>claude --resume &lt;sid&gt;</code> 接管。")

    def _cmd_new(self, chat_id, text, msg_id=None):
        # /new [-m <model>] <描述>
        rest = text[len("/new"):].strip()
        model = None
        if rest.startswith("-m"):
            parts = rest.split(None, 2)   # ['-m', '<model>', '<描述...>']
            if len(parts) < 2:
                self._send(chat_id, "用法：/new [-m opus|sonnet|haiku] <任务描述>")
                return
            model = parts[1]
            rest = parts[2] if len(parts) >= 3 else ""
            # S2⑤：model 必须先过枚举白名单，非法值绝不进 argv
            if model not in self.cfg.ALLOWED_MODELS:
                self._send(chat_id,
                           f"不支持的模型「{model}」。可选："
                           f"{'/'.join(sorted(self.cfg.ALLOWED_MODELS))}")
                return
        if not rest:
            self._send(chat_id, "用法：/new [-m opus|sonnet|haiku] <任务描述>")
            return
        # 未显式 -m 时套用 /model 设定的默认模型（None 则由 CLI 自身默认）
        if model is None:
            model = self._default_model
        # 台账登记 → 入执行队列。prompt 恒为完整原文（S2：进 argv 单元素）。
        # sid 在受理时即预生成：①进度卡构造时就能展示（终端接管靠它）；
        # ②任务排队期即进 active_session_ids，relay 过滤不留窗口期
        sid = str(uuid.uuid4())
        task_id = self.store.create(session_id=sid, title=rest[:60], model=model,
                                    origin="new", status=tasks.STATUS_QUEUED,
                                    chat_id=chat_id)
        self._enqueue({"kind": "new", "task_id": task_id, "chat_id": chat_id,
                       "prompt": rest, "model": model, "title": rest[:60],
                       "session_id": sid})
        # /new 开新会话 → 设为当前会话，此后普通文本都接着它连续对话
        self._set_current(chat_id, task_id)
        # 给用户的 /new 消息加 👀 反应：比"排队中"文字更即时的"已收到、在处理"信号
        if msg_id is not None:
            self.api.set_message_reaction(chat_id, msg_id, "👀")
        self._send(chat_id, f"✅ 已开新会话 #{task_id}，之后直接发消息即可接着聊。")

    # ── /model：默认模型选择（面板）+ 持久化 ─────────────────────────────────
    def _load_default_model(self):
        try:
            with open(self._default_model_file, encoding="utf-8") as f:
                m = f.read().strip()
            return m if m in self.cfg.ALLOWED_MODELS else None
        except OSError:
            return None

    def _save_default_model(self, model):
        # model 为 None → 删除文件（回到 CLI 自身默认）；否则写入（已经过枚举校验）
        try:
            if model is None:
                if os.path.exists(self._default_model_file):
                    os.remove(self._default_model_file)
            else:
                os.makedirs(os.path.dirname(self._default_model_file), exist_ok=True)
                with open(self._default_model_file, "w", encoding="utf-8") as f:
                    f.write(model)
        except OSError as e:
            log.warning("默认模型持久化失败: %s", config.redact(repr(e)))

    def _cmd_model(self, chat_id, text):
        # /model <name> 直接设；/model 无参 → 弹面板选。cur 显示当前默认
        parts = text.split()
        cur = self._default_model or "CLI 默认"
        if len(parts) >= 2:
            name = parts[1]
            if name not in self.cfg.ALLOWED_MODELS:
                self._send(chat_id, f"不支持的模型「{name}」。可选："
                                    f"{'/'.join(sorted(self.cfg.ALLOWED_MODELS))}")
                return
            self._default_model = name
            self._save_default_model(name)
            self._send(chat_id, f"✅ 默认模型已设为 {name}（/new 不带 -m 时用它）。")
            return
        # 面板：每个模型一个按钮 + 一个「清除(用CLI默认)」；callback mdl:<name>，mdl: 为清除
        rows = [[{"text": ("● " if m == self._default_model else "") + m,
                  "callback_data": f"mdl:{m}"}]
                for m in sorted(self.cfg.ALLOWED_MODELS)]
        rows.append([{"text": "清除（用 CLI 默认）", "callback_data": "mdl:"}])
        self._send(chat_id, f"当前默认模型：{cur}\n选择新的默认模型：",
                   reply_markup={"inline_keyboard": rows})

    def _cb_pick_model(self, chat_id, name, cq_id, mid=None):
        # 面板点选默认模型：name 空串=清除；非法值拒绝（防伪造 callback）
        if name and name not in self.cfg.ALLOWED_MODELS:
            self.api.answer_callback_query(cq_id, text="无效模型")
            return
        self._default_model = name or None
        self._save_default_model(self._default_model)
        toast = f"默认模型：{self._default_model}" if self._default_model else "已清除，用 CLI 默认"
        self._collapse_panel(chat_id, mid, f"✅ {toast}")   # 撤面板按钮+写结果
        self.api.answer_callback_query(cq_id, text=toast)

    # ── /rename：重命名任务标题（提升 /tasks、/resume 列表可读性）────────────
    def _cmd_rename(self, chat_id, text):
        # /rename <编号> <新名称>
        parts = text.split(None, 2)
        if len(parts) < 3 or not parts[1].isdigit():
            self._send(chat_id, "用法：/rename <任务编号> <新名称>")
            return
        task = self.store.get(int(parts[1]))
        if not task:
            self._send(chat_id, f"任务 #{parts[1]} 不存在。")
            return
        new_title = parts[2].strip()[:60]
        self.store.update_fields(task["task_id"], title=new_title)
        self._send(chat_id, f"✅ 任务 #{task['task_id']} 已重命名为「{new_title}」。")

    def _cmd_tasks(self, chat_id):
        rows = self.store.list_recent(limit=15)
        if not rows:
            self._send(chat_id, "暂无任务记录。")
            return
        lines = []
        for r in rows:
            emoji = _STATUS_EMOJI.get(r["status"], "❔")
            sid = (r["session_id"] or "")[:8]
            title = r["title"] or "(无标题)"
            lines.append(f"{emoji} #{r['task_id']} {title}  [{sid}]")
        self._send(chat_id, "\n".join(lines))

    def _cmd_say(self, chat_id, text):
        # /say <n> <文本>——n 严格 int
        parts = text.split(None, 2)
        if len(parts) < 3 or not parts[1].isdigit():
            self._send(chat_id, "用法：/say <任务编号> <文本>")
            return
        task = self.store.get(int(parts[1]))
        if not task:
            self._send(chat_id, f"任务 #{parts[1]} 不存在。")
            return
        self._say(chat_id, task, parts[2])

    def _say(self, chat_id, task, body):
        # 接力：running 提示等待；无 session_id 无法 resume；否则入队 resume
        if task["status"] == tasks.STATUS_RUNNING:
            self._send(chat_id,
                       f"任务 #{task['task_id']} 正在运行，请等待完成或先 /cancel。")
            return
        if not task["session_id"]:
            self._send(chat_id,
                       f"任务 #{task['task_id']} 无会话 ID，无法接力。")
            return
        self._set_current(chat_id, task["task_id"])   # /say 或回复卡也切为当前会话
        self.store.update_status(task["task_id"], tasks.STATUS_QUEUED,
                                 last_event="接力续话")
        self._enqueue({"kind": "resume", "task_id": task["task_id"],
                       "chat_id": chat_id, "prompt": body,
                       "model": task["model"], "title": task["title"],
                       "session_id": task["session_id"]})

    def _cmd_cancel(self, chat_id, text):
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            self._send(chat_id, "用法：/cancel <任务编号>")
            return
        task = self.store.get(int(parts[1]))
        if not task:
            self._send(chat_id, f"任务 #{parts[1]} 不存在。")
            return
        if task["status"] == tasks.STATUS_QUEUED:
            # queued 任务直接出队标记（worker 取到发现 cancelled 会跳过）
            self.store.update_status(task["task_id"], tasks.STATUS_CANCELLED,
                                     last_event="排队中取消")
            self._send(chat_id, f"🚫 任务 #{task['task_id']} 已取消（排队中）。")
            return
        # 运行中：杀进程组（runner.cancel killpg TERM→KILL）再迁移状态
        runner.cancel(task["pgid"])
        self.store.update_status(task["task_id"], tasks.STATUS_CANCELLED,
                                 last_event="用户取消")
        self._send(chat_id, f"🚫 任务 #{task['task_id']} 已取消。")

    def _cmd_sessions(self, chat_id):
        self._send_sessions_panel(chat_id, page=0)

    def _send_sessions_panel(self, chat_id, page=0, prefix=None, edit_mid=None):
        # 面板选择（openclaw 式）：每页 8 个会话按钮 + ◀️▶️ 翻页；点按即接管，免手打长 ID
        sessions = self._scan_sessions(limit=_SESSION_SCAN_CAP)
        if not sessions:
            self._send(chat_id, "本工作区暂无 Claude Code 会话记录。")
            return
        rows, text = self._paginate(
            sessions, page, prefix or "选择要接管的会话（点按即接管，无需手打 ID）",
            btn=lambda s: {"text": f"📄 {self._fmt_mtime(s['mtime'])} · {s['summary'][:28]}",
                           "callback_data": f"sess:{s['full']}"},
            nav_prefix="sesspg")
        if edit_mid is not None:
            self.api.edit_message(chat_id, edit_mid, text,
                                  reply_markup={"inline_keyboard": rows})
        else:
            self._send(chat_id, text, reply_markup={"inline_keyboard": rows})

    def _cmd_resume(self, chat_id):
        self._send_tasks_panel(chat_id, page=0)

    def _resumable_tasks(self):
        # 可续接：非运行中、有 session_id（按 task_id 倒序，最近在前）
        return [t for t in self.store.list_recent(limit=_SESSION_SCAN_CAP)
                if t["status"] != tasks.STATUS_RUNNING and t["session_id"]]

    def _send_tasks_panel(self, chat_id, page=0, edit_mid=None):
        tasks_list = self._resumable_tasks()
        if not tasks_list:
            # 台账暂无长程任务 → 回退展示最近会话（点按即接管续话），不留死路
            self._send_sessions_panel(
                chat_id, prefix="还没有本地长程任务。下面是最近的会话，点按即接管续话")
            return
        rows, text = self._paginate(
            tasks_list, page, "选择要续接的任务（点按后“回复”确认消息即可续话）",
            btn=lambda t: {
                "text": f"{_STATUS_EMOJI.get(t['status'], '❔')} #{t['task_id']} "
                        f"{(t['title'] or '')[:26]}",
                "callback_data": f"tsel:{t['task_id']}"},
            nav_prefix="taskpg")
        if edit_mid is not None:
            self.api.edit_message(chat_id, edit_mid, text,
                                  reply_markup={"inline_keyboard": rows})
        else:
            self._send(chat_id, text, reply_markup={"inline_keyboard": rows})

    @staticmethod
    def _paginate(items, page, prompt, btn, nav_prefix):
        # 返回 (inline_keyboard rows, 提示文本)。每页 _PAGE_SIZE 条，附 ◀️▶️ 导航行。
        total = len(items)
        pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * _PAGE_SIZE
        rows = [[btn(it)] for it in items[start:start + _PAGE_SIZE]]
        nav = []
        if page > 0:
            nav.append({"text": "◀️ 上一页", "callback_data": f"{nav_prefix}:{page - 1}"})
        if page < pages - 1:
            nav.append({"text": "下一页 ▶️", "callback_data": f"{nav_prefix}:{page + 1}"})
        if nav:
            rows.append(nav)
        text = f"{prompt}（第 {page + 1}/{pages} 页，共 {total} 条）："
        return rows, text

    def _cmd_attach(self, chat_id, text):
        # /attach <短ID>：唯一前缀匹配 → 登记台账（保留文本入口，与面板等价）
        parts = text.split()
        if len(parts) < 2:
            self._send(chat_id, "用法：/attach <会话短ID>（或直接 /sessions 点按选择）")
            return
        short = parts[1]
        matches = [s for s in self._all_session_stems() if s.startswith(short)]
        if len(matches) == 0:
            self._send(chat_id, f"未找到以「{short}」开头的会话。")
            return
        if len(matches) > 1:
            self._send(chat_id, f"短ID「{short}」不唯一，请提供更多字符。")
            return
        tid = self._attach_session(chat_id, matches[0])
        if tid:
            self._send(chat_id, f"✅ 已切到会话 #{tid}，直接发消息即可续话。")

    def _attach_session(self, chat_id, full):
        """接管会话 full → 登记台账 → 「切为当前会话」。
        返回 task_id；会话已有活跃任务时返回 None 并提示（防双端 resume）。"""
        if full in self.store.active_session_ids():
            self._send(chat_id, "该会话已有活跃任务，拒绝重复接管（防双端 resume）。")
            return None
        summary = self._first_user_summary(self._session_path(full))
        task_id = self.store.create(session_id=full, title=summary,
                                    origin="attach", status=tasks.STATUS_QUEUED,
                                    chat_id=chat_id)
        self._set_current(chat_id, task_id)   # 切为当前会话：此后普通文本接着它聊
        return task_id

    # ── /sessions 辅助 ──────────────────────────────────────────────────────
    def _session_path(self, stem):
        return os.path.join(self.cfg.SESSIONS_PROJECT_DIR, stem + ".jsonl")

    def _all_session_stems(self):
        d = self.cfg.SESSIONS_PROJECT_DIR
        try:
            return [f[:-len(".jsonl")] for f in os.listdir(d) if f.endswith(".jsonl")]
        except OSError:
            return []

    def _scan_sessions(self, limit=10):
        d = self.cfg.SESSIONS_PROJECT_DIR
        try:
            files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
        except OSError:
            return []
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        out = []
        for p in files[:limit]:
            stem = os.path.basename(p)[:-len(".jsonl")]
            out.append({
                "short": stem[:8], "full": stem,
                "mtime": os.path.getmtime(p),
                "summary": self._first_user_summary(p),
            })
        return out

    @staticmethod
    def _fmt_mtime(mtime):
        import datetime
        return datetime.datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")

    @staticmethod
    def _first_user_summary(path, max_lines=20, maxlen=60):
        """读会话文件首几行，取首条 type=='user' 的文本摘要（宽容解析）。

        content 可能是 str 或 [{'type':'text','text':...}] 列表；解析失败返回占位。
        """
        try:
            with open(path, encoding="utf-8") as f:
                for _ in range(max_lines):
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "user":
                        continue
                    content = (obj.get("message") or {}).get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text += part.get("text", "")
                            elif isinstance(part, str):
                                text += part
                    text = text.strip()
                    if text:
                        return text[:maxlen]
            return "(无法解析)"
        except OSError:
            return "(无法解析)"

    def _task_by_progress_msg(self, message_id):
        if message_id is None:
            return None
        for r in self.store.list_recent(limit=50):
            if r.get("progress_msg_id") == message_id:
                return r
        return None

    # ── offset 持久化（v1 同款语义，新 offset 文件避免与 v1 冲突）─────────────
    def _load_offset(self):
        try:
            with open(self.cfg.OFFSET_FILE) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return 0

    def _save_offset(self, n):
        parent = os.path.dirname(self.cfg.OFFSET_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.cfg.OFFSET_FILE, "w") as f:
            f.write(str(n))

    # ── CLI 版本自检 ────────────────────────────────────────────────────────
    def _version_selfcheck(self):
        try:
            out = subprocess.run([runner.CLAUDE_BIN, "--version"],
                                 capture_output=True, text=True, timeout=15)
            cur = (out.stdout or "").strip()
        except Exception as e:   # noqa: BLE001
            log.warning("claude --version 自检失败: %s", config.redact(repr(e)))
            return
        prev = None
        try:
            with open(self.cfg.CLI_VERSION_FILE) as f:
                prev = f.read().strip()
        except OSError:
            prev = None
        if prev and cur and prev != cur:
            self._send(self.cfg.CHAT_ID,
                       f"⚠️ 检测到 Claude CLI 版本变化（{prev} → {cur}），"
                       f"stream-json/hook 协议可能变更，建议回归实测。")
        try:
            parent = os.path.dirname(self.cfg.CLI_VERSION_FILE)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.cfg.CLI_VERSION_FILE, "w") as f:
                f.write(cur)
        except OSError:
            pass

    # ── 主循环 ──────────────────────────────────────────────────────────────
    def run(self):
        log.info("tg-longrange daemon 启动")
        self.api.delete_webhook()               # 清 webhook 残留（R4）
        self.api.set_my_commands(BOT_COMMANDS)  # 注册「/」自动补全菜单（覆盖旧残留）
        self._version_selfcheck()               # 版本变化告警（T-R1a）
        rec = self.store.recover_on_start()      # 台账恢复（R3）
        if rec["interrupted"] or rec["orphaned"]:
            self._send(self.cfg.CHAT_ID,
                       f"🔄 启动恢复：中断任务 {rec['interrupted']}，"
                       f"孤儿任务 {rec['orphaned']}（可 /say 续接或 /cancel）。")
        # 上线播报：让 Owner 确认新版已接管（v1 有此播报，保持行为）
        self._send(self.cfg.CHAT_ID,
                   "🚀 长程会话已上线（tg-longrange）。发 /help 看用法，"
                   "/new 起长程任务，普通文本仍是即时问答。")
        self.start_workers()
        if self._relay is not None:
            self._relay.start()                 # 审批中继线程
        import time
        while not self._stop:
            offset = self._load_offset()
            updates = self.api.get_updates(offset + 1)
            if updates is None:
                time.sleep(5)                   # 网络异常 5s 重试（v1 语义）
                continue
            for u in updates:
                try:
                    self.handle_update(u)
                except Exception as e:   # noqa: BLE001 — 单条 update 异常不拖垮主循环
                    log.error("处理 update 异常: %s", config.redact(repr(e)))
                self._save_offset(u["update_id"])


def main():   # pragma: no cover — 投运入口
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [tg-lr] %(message)s", datefmt="%H:%M:%S")
    if not config.BOT_TOKEN:
        raise SystemExit(
            "未取得 bot token：设置环境变量 TGLR_BOT_TOKEN，"
            "或写入 ~/.claude-telegram-longrange/config.json 的 bot_token 字段。")
    api = TgApi(config.BOT_TOKEN)
    store = tasks.TaskStore()
    Daemon(api, store, config).run()


if __name__ == "__main__":   # pragma: no cover
    main()
