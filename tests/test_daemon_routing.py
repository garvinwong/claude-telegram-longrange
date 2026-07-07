#!/usr/bin/env python3
# test_daemon_routing.py — 路由层单测（pytest）
#
# 约束（红线）：TG API 全 mock（FakeApi），runner.start/resume/cancel 用 monkeypatch 假桩，
#   短问答 ask_claude 也 mock——绝不真实网络、绝不起真 claude、绝不投运。DB 全用 tmp_path。
#   单测内 sleep 上限 2s；worker 线程用事件/短轮询等待。
#
# 运行：python3 -m pytest apps/tg-longrange/tests/test_daemon_routing.py -v

import os
import sys
import threading
import time

import pytest

# 无包结构，把 apps/tg-longrange 挂上 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config   # noqa: E402
import daemon   # noqa: E402
import progress  # noqa: E402
import runner   # noqa: E402
import session_lock  # noqa: E402
import tasks     # noqa: E402

# 在任何 monkeypatch 之前抓住真实进度卡类，供集成用例还原（fixture 会把它换成 _NoopCard）
_REAL_PROGRESS_CARD = progress.ProgressCard


class _NoopCard:
    """路由测试用无害进度卡：不发任何 TG 消息，让路由断言不受 worker 完成时的
    终态通知干扰（进度卡真实行为由 test_progress.py 覆盖，集成由专门用例覆盖）。"""

    def __init__(self, *a, **k):
        self.message_id = None

    def handle_event(self, event):
        pass

    def finish(self, status, note=None):
        pass


# ── 假 TG 网络层 ─────────────────────────────────────────────────────────
class FakeApi:
    """记录所有调用；绝不联网。"""

    def __init__(self):
        self.sent = []
        self.edited = []
        self.actions = []
        self.answered = []
        self.reactions = []
        self.deleted = 0
        self._mid = 1000

    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML"):
        self.sent.append({"chat_id": chat_id, "text": text,
                          "reply_markup": reply_markup})
        self._mid += 1
        return self._mid

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id,
                            "text": text})
        return {"ok": True, "retry_after": None}

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered.append((callback_query_id, text))

    def get_file(self, file_id):
        return None

    def download(self, file_path, save_path):
        return False

    def get_updates(self, offset, timeout=25):
        return []

    def delete_webhook(self):
        self.deleted += 1

    def set_my_commands(self, commands):
        self.commands = commands

    def set_message_reaction(self, chat_id, message_id, emoji="👀"):
        self.reactions.append((chat_id, message_id, emoji))

    # 便捷断言辅助
    def last_text(self):
        return self.sent[-1]["text"] if self.sent else None

    def all_text(self):
        return "\n".join(s["text"] for s in self.sent)


class Bundle:
    def __init__(self, dae, api, store, rcalls, qa_calls):
        self.d = dae
        self.api = api
        self.store = store
        self.rcalls = rcalls
        self.qa_calls = qa_calls


WHITELIST_UID = 123456789


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    store = tasks.TaskStore(db_path=str(tmp_path / "tasks.db"))
    api = FakeApi()
    rcalls = {"start": [], "resume": [], "cancel": []}

    def fake_start(prompt, model=None, on_event=None, session_id=None,
                   cwd=None, timeout=None):
        rcalls["start"].append({"prompt": prompt, "model": model,
                                "session_id": session_id, "cwd": cwd})
        sid = session_id or f"gen-sid-{len(rcalls['start'])}"
        if on_event:
            on_event({"type": "spawn", "session_id": sid, "pid": 4321, "pgid": 4321})
            on_event({"type": "result", "ok": True, "result_text": "done",
                      "permission_denials": [], "session_id": sid,
                      "total_cost_usd": 0.0})
        return {"session_id": sid, "pid": 4321, "pgid": 4321, "returncode": 0}

    def fake_resume(sid, prompt, model=None, on_event=None, cwd=None, timeout=None):
        rcalls["resume"].append({"session_id": sid, "prompt": prompt, "model": model})
        if on_event:
            on_event({"type": "spawn", "session_id": sid, "pid": 4322, "pgid": 4322})
            on_event({"type": "result", "ok": True, "result_text": "resumed",
                      "permission_denials": [], "session_id": sid})
        return {"session_id": sid, "pid": 4322, "pgid": 4322, "returncode": 0}

    def fake_cancel(pgid, grace=5.0):
        rcalls["cancel"].append(pgid)

    monkeypatch.setattr(runner, "start", fake_start)
    monkeypatch.setattr(runner, "resume", fake_resume)
    monkeypatch.setattr(runner, "cancel", fake_cancel)

    qa_calls = []
    monkeypatch.setattr(daemon, "ask_claude",
                        lambda text, **k: (qa_calls.append(text) or "QA-REPLY"))

    # 路由测试默认用无害进度卡（见 _NoopCard）；需验证真实进度卡集成的用例自行还原
    monkeypatch.setattr(progress, "ProgressCard", _NoopCard)
    # 隔离默认模型/offset 文件到 tmp，绝不污染真实状态目录（含线上 daemon）
    monkeypatch.setattr(config, "OFFSET_FILE", str(tmp_path / "offset"))
    # config 的白名单来自环境变量，测试环境为空——注入占位白名单，等价线上鉴权
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {WHITELIST_UID})

    dae = daemon.Daemon(api, store, config)
    b = Bundle(dae, api, store, rcalls, qa_calls)
    yield b
    dae.shutdown()
    store.close()


def wait_for(cond, timeout=2.0, interval=0.01):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def msg_update(text=None, uid=WHITELIST_UID, chat_type="private",
               chat_id=WHITELIST_UID, reply_to=None, update_id=1,
               photo=None, caption=None, first_name="Owner"):
    m = {"message_id": 10, "from": {"id": uid},
         "chat": {"id": chat_id, "type": chat_type, "first_name": first_name}}
    if text is not None:
        m["text"] = text
    if photo is not None:
        m["photo"] = photo
    if caption is not None:
        m["caption"] = caption
    if reply_to is not None:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": m}


# ── ① 非白名单 from.id → 无任何路由动作（T-S1a）──────────────────────────
def test_non_whitelist_dropped(bundle):
    bundle.d.handle_update(msg_update(text="做点事", uid=99999999))
    assert bundle.api.sent == []
    assert bundle.qa_calls == []
    assert bundle.rcalls["start"] == []


# ── ② 群聊消息丢弃（T-S1d）────────────────────────────────────────────────
def test_group_chat_dropped(bundle):
    bundle.d.handle_update(msg_update(text="/tasks", chat_type="group"))
    assert bundle.api.sent == []
    assert bundle.qa_calls == []


# ── ③ /new 创建任务入队并最终调 runner.start ──────────────────────────────
def test_new_enqueues_and_runs(bundle):
    bundle.d.handle_update(msg_update(text="/new 写一份周报"))
    assert wait_for(lambda: len(bundle.rcalls["start"]) == 1), "runner.start 未被调用"
    assert bundle.rcalls["start"][0]["prompt"] == "写一份周报"
    # 任务落终态 done；sid 由 daemon 受理时预生成（uuid），并原样传给 runner.start
    assert wait_for(lambda: bundle.store.get(1)["status"] == tasks.STATUS_DONE)
    sid = bundle.store.get(1)["session_id"]
    assert sid and len(sid) == 36, "受理时应预生成 uuid 作 session_id"
    assert bundle.rcalls["start"][0]["session_id"] == sid, "预生成 sid 必须透传 runner.start"


# ── ⑤ /new -m opus 透传 model；/new -m gpt5 拒绝（S2⑤）────────────────────
def test_new_model_passthrough_and_reject(bundle):
    bundle.d.handle_update(msg_update(text="/new -m opus 起个任务"))
    assert wait_for(lambda: len(bundle.rcalls["start"]) == 1)
    assert bundle.rcalls["start"][0]["model"] == "opus"
    assert bundle.rcalls["start"][0]["prompt"] == "起个任务"

    # 非枚举 model：拒绝、不进 argv、不新增 runner 调用
    n_before = len(bundle.rcalls["start"])
    bundle.d.handle_update(msg_update(text="/new -m gpt5 危险任务", update_id=2))
    time.sleep(0.15)
    assert len(bundle.rcalls["start"]) == n_before
    assert "gpt5" in bundle.api.last_text()


# ── ⑥ /say 路由到正确 task 的 resume ──────────────────────────────────────
def test_say_routes_to_resume(bundle):
    tid = bundle.store.create(session_id="say-sid", title="旧任务",
                              status=tasks.STATUS_DONE, chat_id=str(WHITELIST_UID))
    bundle.d.handle_update(msg_update(text=f"/say {tid} 继续深入"))
    assert wait_for(lambda: len(bundle.rcalls["resume"]) == 1)
    assert bundle.rcalls["resume"][0]["session_id"] == "say-sid"
    assert bundle.rcalls["resume"][0]["prompt"] == "继续深入"


# ── ⑦ 对进度卡回复 等价 /say ───────────────────────────────────────────────
def test_reply_to_progress_card_is_say(bundle):
    tid = bundle.store.create(session_id="rep-sid", title="进度卡任务",
                              status=tasks.STATUS_DONE, chat_id=str(WHITELIST_UID))
    bundle.store.update_fields(tid, progress_msg_id=555)
    bundle.d.handle_update(msg_update(text="接着把结论写完", reply_to=555))
    assert wait_for(lambda: len(bundle.rcalls["resume"]) == 1)
    assert bundle.rcalls["resume"][0]["session_id"] == "rep-sid"
    assert bundle.rcalls["resume"][0]["prompt"] == "接着把结论写完"


# ── ⑧ /cancel 调 runner.cancel(pgid) 且状态迁移 ───────────────────────────
def test_cancel_kills_and_transitions(bundle):
    tid = bundle.store.create(session_id="c-sid", title="运行中",
                              status=tasks.STATUS_RUNNING, chat_id=str(WHITELIST_UID))
    bundle.store.update_fields(tid, pgid=7777)
    bundle.d.handle_update(msg_update(text=f"/cancel {tid}"))
    assert bundle.rcalls["cancel"] == [7777]
    assert bundle.store.get(tid)["status"] == tasks.STATUS_CANCELLED


# ── ⑨ 队列 FIFO：并发 2 占满后第 3 个保持 queued（T-R5b 路由半边）─────────
def test_fifo_third_task_stays_queued(bundle, monkeypatch):
    started = []
    release = threading.Event()

    def blocking_start(prompt, model=None, on_event=None, session_id=None,
                       cwd=None, timeout=None):
        started.append(prompt)
        release.wait(timeout=5)
        return {"session_id": "s", "pid": 1, "pgid": 1, "returncode": 0}

    monkeypatch.setattr(runner, "start", blocking_start)

    bundle.d.handle_update(msg_update(text="/new t1", update_id=1))
    bundle.d.handle_update(msg_update(text="/new t2", update_id=2))
    bundle.d.handle_update(msg_update(text="/new t3", update_id=3))

    # 前两个占满并发，第三个进不去
    assert wait_for(lambda: len(started) == 2), "并发 2 未被占满"
    time.sleep(0.15)
    assert len(started) == 2, "第 3 个任务不应启动"
    assert bundle.store.get(3)["status"] == tasks.STATUS_QUEUED
    assert bundle.store.get(1)["status"] == tasks.STATUS_RUNNING
    assert bundle.store.get(2)["status"] == tasks.STATUS_RUNNING

    release.set()   # 放行，令 worker 线程退出，便于清理


# ── ⑩ /sessions 解析构造的假 projects 目录 ────────────────────────────────
def _write_session(dirpath, uuid, user_text, mtime):
    p = os.path.join(dirpath, uuid + ".jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"type":"system","subtype":"init"}\n')
        f.write('{"type":"user","message":{"role":"user","content":"%s"}}\n' % user_text)
        f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n')
    os.utime(p, (mtime, mtime))
    return p


def test_sessions_lists_parsed(bundle, tmp_path, monkeypatch):
    sdir = tmp_path / "projects"
    sdir.mkdir()
    base = time.time()
    _write_session(str(sdir), "aaaaaaaa-1111-2222-3333-444444444444", "问题甲", base - 30)
    _write_session(str(sdir), "bbbbbbbb-1111-2222-3333-444444444444", "问题乙", base - 20)
    _write_session(str(sdir), "cccccccc-1111-2222-3333-444444444444", "问题丙", base - 10)
    monkeypatch.setattr(config, "SESSIONS_PROJECT_DIR", str(sdir))

    bundle.d.handle_update(msg_update(text="/sessions"))
    # 会话现以 inline keyboard 按钮呈现（免手打长 ID），callback 带完整 uuid
    kb = bundle.api.sent[-1]["reply_markup"]["inline_keyboard"]
    cbs = [row[0]["callback_data"] for row in kb]
    assert "sess:aaaaaaaa-1111-2222-3333-444444444444" in cbs
    assert "sess:bbbbbbbb-1111-2222-3333-444444444444" in cbs
    assert "sess:cccccccc-1111-2222-3333-444444444444" in cbs
    # 按钮文案含摘要，且 callback_data < 64 字节
    labels = " ".join(row[0]["text"] for row in kb)
    assert "问题甲" in labels and "问题丙" in labels
    for c in cbs:
        assert len(c.encode()) < 64


def test_sessions_button_pick_attaches(bundle, tmp_path, monkeypatch):
    sdir = tmp_path / "projects"
    sdir.mkdir()
    uuid_a = "abcd1234-1111-2222-3333-444444444444"
    _write_session(str(sdir), uuid_a, "点选接管我", time.time())
    monkeypatch.setattr(config, "SESSIONS_PROJECT_DIR", str(sdir))
    # 模拟点按 sess: 按钮
    cq = {"callback_query": {"id": "cb1", "from": {"id": WHITELIST_UID},
                             "message": {"chat": {"id": WHITELIST_UID}},
                             "data": f"sess:{uuid_a}"}}
    bundle.d.handle_update(cq)
    row = bundle.store.get_by_session(uuid_a)
    assert row is not None and row["origin"] == "attach"
    # 接管即「切为当前会话」；toast 提示已切
    assert bundle.d._current[str(WHITELIST_UID)] == row["task_id"]
    assert bundle.api.answered[-1][1] == "已切到该会话"
    # 此后直接发普通文本 → 接着该会话 resume（无需回复任何消息）
    bundle.d.handle_update(msg_update(text="继续写完"))
    assert wait_for(lambda: len(bundle.rcalls["resume"]) == 1)
    assert bundle.rcalls["resume"][0]["session_id"] == uuid_a


def test_resume_empty_falls_back_to_sessions(bundle, tmp_path, monkeypatch):
    # 台账无可续任务时，/resume 不再死路，回退展示会话面板（sess: 按钮）
    sdir = tmp_path / "projects"
    sdir.mkdir()
    _write_session(str(sdir), "eeee1111-1111-2222-3333-444444444444", "回退会话", time.time())
    monkeypatch.setattr(config, "SESSIONS_PROJECT_DIR", str(sdir))
    bundle.d.handle_update(msg_update(text="/resume"))
    kb = bundle.api.sent[-1]["reply_markup"]["inline_keyboard"]
    cbs = [row[0]["callback_data"] for row in kb]
    assert any(c.startswith("sess:") for c in cbs)   # 回退到会话选择
    assert "会话" in bundle.api.last_text()


def test_resume_panel_lists_resumable_tasks(bundle):
    r_id = bundle.store.create(session_id="rs-sid", title="可续任务",
                               status=tasks.STATUS_DONE, chat_id=str(WHITELIST_UID))
    bundle.store.create(session_id="run-sid", title="运行中不列",
                        status=tasks.STATUS_RUNNING, chat_id=str(WHITELIST_UID))
    bundle.d.handle_update(msg_update(text="/resume"))
    kb = bundle.api.sent[-1]["reply_markup"]["inline_keyboard"]
    cbs = [row[0]["callback_data"] for row in kb]
    assert f"tsel:{r_id}" in cbs          # 可续任务在面板
    assert not any(c.endswith("run-sid") for c in cbs)  # 运行中的不列（无 tsel）
    # 点选 → 切为当前会话，此后普通文本接着它聊
    cq = {"callback_query": {"id": "cb2", "from": {"id": WHITELIST_UID},
                             "message": {"chat": {"id": WHITELIST_UID}},
                             "data": f"tsel:{r_id}"}}
    bundle.d.handle_update(cq)
    assert bundle.d._current[str(WHITELIST_UID)] == r_id
    assert bundle.api.answered[-1][1] == "已切到该会话"
    bundle.d.handle_update(msg_update(text="接着说"))
    assert wait_for(lambda: len(bundle.rcalls["resume"]) == 1)
    assert bundle.rcalls["resume"][0]["session_id"] == "rs-sid"


def test_bot_commands_menu_valid_and_covers_router():
    # 「/」菜单命令须格式合法（小写/≤32/描述≤256），且都是路由真实处理的命令
    handled = {"new", "resume", "sessions", "tasks", "say", "cancel", "attach",
               "help", "model", "rename", "current", "detach", "watch", "unwatch", "extend"}
    seen = set()
    for c in daemon.BOT_COMMANDS:
        name = c["command"]
        assert name.islower() and 1 <= len(name) <= 32 and name.isidentifier()
        assert 1 <= len(c["description"]) <= 256
        assert name in handled, f"菜单命令 /{name} 未在路由实现"
        seen.add(name)
    # 面板类关键命令必须在菜单（本次问题的核心）
    assert {"sessions", "resume", "help"} <= seen


def test_model_panel_and_pick_sets_default(bundle, tmp_path, monkeypatch):
    # /model 无参 → 弹面板（每模型一按钮 + 清除）
    bundle.d.handle_update(msg_update(text="/model"))
    kb = bundle.api.sent[-1]["reply_markup"]["inline_keyboard"]
    cbs = [row[0]["callback_data"] for row in kb]
    assert "mdl:opus" in cbs and "mdl:sonnet" in cbs and "mdl:haiku" in cbs
    assert "mdl:" in cbs  # 清除项
    # 点选 opus → 设为默认并持久化
    cq = {"callback_query": {"id": "m1", "from": {"id": WHITELIST_UID},
                             "message": {"chat": {"id": WHITELIST_UID}},
                             "data": "mdl:opus"}}
    bundle.d.handle_update(cq)
    assert bundle.d._default_model == "opus"
    assert bundle.api.answered[-1][1].endswith("opus")
    # 此后 /new 不带 -m → 用默认 opus
    bundle.d.handle_update(msg_update(text="/new 用默认模型跑", update_id=9))
    assert wait_for(lambda: len(bundle.rcalls["start"]) == 1)
    assert bundle.rcalls["start"][0]["model"] == "opus"
    # 清除 → 回 CLI 默认（None）
    cq2 = {"callback_query": {"id": "m2", "from": {"id": WHITELIST_UID},
                             "message": {"chat": {"id": WHITELIST_UID}}, "data": "mdl:"}}
    bundle.d.handle_update(cq2)
    assert bundle.d._default_model is None


def test_model_explicit_m_overrides_default(bundle):
    bundle.d._default_model = "opus"
    bundle.d.handle_update(msg_update(text="/new -m haiku 明确指定"))
    assert wait_for(lambda: len(bundle.rcalls["start"]) == 1)
    assert bundle.rcalls["start"][0]["model"] == "haiku"  # -m 优先于默认


def test_model_persists_across_reload(bundle, tmp_path):
    bundle.d.handle_update({"callback_query": {"id": "mp", "from": {"id": WHITELIST_UID},
                            "message": {"chat": {"id": WHITELIST_UID}}, "data": "mdl:sonnet"}})
    # 新建一个共享同 cfg（同 OFFSET_FILE 目录）的 daemon，应从文件读回默认
    d2 = daemon.Daemon(bundle.api, bundle.store, config)
    assert d2._default_model == "sonnet"
    d2.shutdown()


def test_rename_updates_title(bundle):
    tid = bundle.store.create(session_id="rn-sid", title="自动截取的长描述",
                              status=tasks.STATUS_DONE, chat_id=str(WHITELIST_UID))
    bundle.d.handle_update(msg_update(text=f"/rename {tid} 周报"))
    assert bundle.store.get(tid)["title"] == "周报"
    # 格式错误 → 提示用法，不改
    bundle.d.handle_update(msg_update(text="/rename abc x", update_id=2))
    assert "用法" in bundle.api.last_text()


def test_sessions_pagination(bundle, tmp_path, monkeypatch):
    # 造 20 个会话 → 每页 8，共 3 页；首页有「下一页」无「上一页」
    sdir = tmp_path / "projects"
    sdir.mkdir()
    base = time.time()
    for i in range(20):
        _write_session(str(sdir), f"{i:08d}-1111-2222-3333-444444444444",
                       f"会话{i}", base - i)   # i 越小越新
    monkeypatch.setattr(config, "SESSIONS_PROJECT_DIR", str(sdir))

    bundle.d.handle_update(msg_update(text="/sessions"))
    kb = bundle.api.sent[-1]["reply_markup"]["inline_keyboard"]
    sess_btns = [r[0] for r in kb if r[0]["callback_data"].startswith("sess:")]
    nav = kb[-1]
    assert len(sess_btns) == 8                       # 每页 8 条
    assert "第 1/3 页" in bundle.api.last_text()
    assert any(b["callback_data"] == "sesspg:1" for b in nav)   # 有下一页
    assert not any("sesspg:-" in b["callback_data"] for b in nav)  # 首页无上一页

    # 翻到第 2 页：编辑同一条消息（不新发）
    n_sent_before = len(bundle.api.sent)
    cq = {"callback_query": {"id": "pg", "from": {"id": WHITELIST_UID},
                             "message": {"chat": {"id": WHITELIST_UID}, "message_id": 777},
                             "data": "sesspg:1"}}
    bundle.d.handle_update(cq)
    assert len(bundle.api.sent) == n_sent_before      # 未新发消息
    assert bundle.api.edited[-1]["message_id"] == 777  # 编辑了面板消息
    assert "第 2/3 页" in bundle.api.edited[-1]["text"]


def test_new_adds_eyes_reaction(bundle):
    # /new 收到后给用户消息加 👀 反应（比"排队中"文字更即时的处理信号）
    bundle.d.handle_update(msg_update(text="/new 干活"))
    assert any(r[2] == "👀" and r[1] == 10 for r in bundle.api.reactions)


def test_pick_session_collapses_panel_and_switches_current(bundle, tmp_path, monkeypatch):
    sdir = tmp_path / "projects"
    sdir.mkdir()
    uuid_a = "cccc9999-1111-2222-3333-444444444444"
    _write_session(str(sdir), uuid_a, "点我接管", time.time())
    monkeypatch.setattr(config, "SESSIONS_PROJECT_DIR", str(sdir))
    # 带 message_id 的面板点击 → 面板消息被就地改写（撤按钮）+ 切为当前会话
    cq = {"callback_query": {"id": "c", "from": {"id": WHITELIST_UID},
                             "message": {"chat": {"id": WHITELIST_UID}, "message_id": 888},
                             "data": f"sess:{uuid_a}"}}
    bundle.d.handle_update(cq)
    assert any(e["message_id"] == 888 and "已切到会话" in e["text"] for e in bundle.api.edited)
    row = bundle.store.get_by_session(uuid_a)
    assert bundle.d._current[str(WHITELIST_UID)] == row["task_id"]


def test_pick_model_collapses_panel(bundle):
    cq = {"callback_query": {"id": "m", "from": {"id": WHITELIST_UID},
                             "message": {"chat": {"id": WHITELIST_UID}, "message_id": 999},
                             "data": "mdl:sonnet"}}
    bundle.d.handle_update(cq)
    assert bundle.d._default_model == "sonnet"
    assert any(e["message_id"] == 999 and "sonnet" in e["text"] for e in bundle.api.edited)


def test_plain_text_starts_then_continues_conversation(bundle):
    # 首条普通文本 → 新建对话会话(origin=chat)并 start；设为当前会话
    bundle.d.handle_update(msg_update(text="帮我看看 crowdsec 配置", update_id=1))
    assert wait_for(lambda: bundle.store.get(1) and bundle.store.get(1)["status"] == tasks.STATUS_DONE)
    t1 = bundle.store.get(1)
    assert t1["origin"] == "chat"
    assert bundle.d._current[str(WHITELIST_UID)] == 1
    # 收到消息即加 👀（处理中信号）
    assert any(r[2] == "👀" for r in bundle.api.reactions)
    # 第二条普通文本 → 接着同一会话 resume（有记忆），不新建
    sid = t1["session_id"]
    bundle.d.handle_update(msg_update(text="那第二点呢", update_id=2))
    assert wait_for(lambda: len(bundle.rcalls["resume"]) == 1)
    assert bundle.rcalls["resume"][0]["session_id"] == sid
    assert len(bundle.rcalls["start"]) == 1   # 未再新建


def test_new_opens_fresh_then_plain_continues_it(bundle):
    bundle.d.handle_update(msg_update(text="/new 重构支付重试", update_id=1))
    assert wait_for(lambda: bundle.store.get(1)["status"] == tasks.STATUS_DONE)
    sid = bundle.store.get(1)["session_id"]
    assert bundle.d._current[str(WHITELIST_UID)] == 1
    bundle.d.handle_update(msg_update(text="加上单元测试", update_id=2))
    assert wait_for(lambda: len(bundle.rcalls["resume"]) == 1)
    assert bundle.rcalls["resume"][0]["session_id"] == sid


def test_converse_while_running_asks_to_wait(bundle):
    tid = bundle.store.create(session_id="run-sid", title="在跑",
                              status=tasks.STATUS_RUNNING, chat_id=str(WHITELIST_UID))
    bundle.d._current[str(WHITELIST_UID)] = tid
    bundle.d.handle_update(msg_update(text="再补一句"))
    assert "还在处理" in bundle.api.last_text()
    assert bundle.rcalls["resume"] == [] and bundle.rcalls["start"] == []


def test_picker_callback_non_whitelist_rejected(bundle):
    cq = {"callback_query": {"id": "cbX", "from": {"id": 999},
                             "message": {"chat": {"id": 999}},
                             "data": "sess:abcd1234-1111-2222-3333-444444444444"}}
    bundle.d.handle_update(cq)
    # 无接管动作、无 runner 调用，仅静默 answer
    assert bundle.rcalls["resume"] == [] and bundle.rcalls["start"] == []


# ── ⑪ /attach 唯一命中登记 / 活跃拒绝 / 不存在报错（T-C4a/c）────────────────
def test_attach_register_reject_notfound(bundle, tmp_path, monkeypatch):
    sdir = tmp_path / "projects"
    sdir.mkdir()
    uuid_a = "abcd1234-1111-2222-3333-444444444444"
    uuid_b = "bbbb5678-1111-2222-3333-444444444444"
    base = time.time()
    _write_session(str(sdir), uuid_a, "接管我甲", base - 20)
    _write_session(str(sdir), uuid_b, "接管我乙", base - 10)
    monkeypatch.setattr(config, "SESSIONS_PROJECT_DIR", str(sdir))

    # 唯一前缀命中 → 登记 origin=attach
    bundle.d.handle_update(msg_update(text="/attach abcd1234"))
    row = bundle.store.get_by_session(uuid_a)
    assert row is not None and row["origin"] == "attach"
    assert row["title"] == "接管我甲"

    # 对已有活跃任务的 session 拒绝（C4 互斥）
    bundle.store.create(session_id=uuid_b, title="活跃",
                        status=tasks.STATUS_RUNNING, chat_id=str(WHITELIST_UID))
    n_before = len(bundle.store.list_recent(limit=50))
    bundle.d.handle_update(msg_update(text="/attach bbbb5678", update_id=2))
    assert "拒绝" in bundle.api.last_text()
    # 未新增 attach 任务
    assert len(bundle.store.list_recent(limit=50)) == n_before

    # 不存在短 ID → 友好报错
    bundle.d.handle_update(msg_update(text="/attach zzzzzzzz", update_id=3))
    assert "未找到" in bundle.api.last_text()


# ── ⑫ 注入字符串作 /new 描述 → 按原文进 runner.start，无 shell 调用（T-S2a）──
def test_injection_string_passed_verbatim(bundle):
    payload = '"; rm -rf ~ #'
    bundle.d.handle_update(msg_update(text="/new " + payload))
    assert wait_for(lambda: len(bundle.rcalls["start"]) == 1)
    # 结构性保证：runner 全程 argv 列表 + shell=False，此处断言原文逐字进 prompt
    assert bundle.rcalls["start"][0]["prompt"] == payload


# ── 附加：callback_query 非白名单 from.id → 静默 answer、不落桥 ────────────
def test_callback_non_whitelist_silently_answered(bundle):
    cq = {"callback_query": {"id": "cbq1", "from": {"id": 424242},
                             "data": "approve:xxx"}}
    bundle.d.handle_update(cq)
    assert bundle.api.answered == [("cbq1", None)]


# ── 附加：进程结束但无 result/error 事件 → 台账收口为 interrupted（集成修复）──
def test_terminal_closeout_interrupted_when_no_result(bundle, monkeypatch):
    # 模拟子进程被外部杀死：只发 spawn，不发 result/error（stdout 直接 EOF）
    def silent_start(prompt, model=None, on_event=None, session_id=None,
                     cwd=None, timeout=None):
        sid = session_id or "silent-sid"
        if on_event:
            on_event({"type": "spawn", "session_id": sid, "pid": 9, "pgid": 9})
        return {"session_id": sid, "pid": 9, "pgid": 9, "returncode": -15}

    monkeypatch.setattr(runner, "start", silent_start)
    bundle.d.handle_update(msg_update(text="/new 会被杀掉的任务"))
    # worker 结束后应把仍 running 的台账收口为 interrupted，可 /say 接力
    assert wait_for(lambda: bundle.store.get(1)["status"] == tasks.STATUS_INTERRUPTED)
    assert "接力" in (bundle.store.get(1)["last_event"] or "")


# ── 附加：/cancel 后迟到的 result 事件不得把 cancelled 覆盖为 done（竞态防护）──
def test_late_result_does_not_override_cancelled(bundle, monkeypatch):
    # 任务运行中；worker 内在 result 事件到达前，台账已被 /cancel 置 cancelled
    gate = threading.Event()

    def racy_start(prompt, model=None, on_event=None, session_id=None,
                   cwd=None, timeout=None):
        sid = session_id or "race-sid"
        if on_event:
            on_event({"type": "spawn", "session_id": sid, "pid": 8, "pgid": 8})
        gate.wait(timeout=5)   # 等测试把台账置 cancelled 后再吐 result
        if on_event:
            on_event({"type": "result", "ok": True, "result_text": "done",
                      "permission_denials": [], "session_id": sid})
        return {"session_id": sid, "pid": 8, "pgid": 8, "returncode": 0}

    monkeypatch.setattr(runner, "start", racy_start)
    bundle.d.handle_update(msg_update(text="/new 边跑边被取消"))
    assert wait_for(lambda: bundle.store.get(1)["pgid"] == 8)   # spawn 已落库
    bundle.store.update_status(1, tasks.STATUS_CANCELLED, last_event="用户取消")
    gate.set()   # 放行迟到的 result
    time.sleep(0.2)
    # 迟到 result 必须被 cancelled 挡住，不得翻成 done
    assert bundle.store.get(1)["status"] == tasks.STATUS_CANCELLED


# ── 附加：真实 ProgressCard 集成——任务完成时 card.finish 另发终态通知 ────────
def test_real_progress_card_finish_notifies(bundle, monkeypatch):
    # 还原真实 ProgressCard（fixture 默认用 _NoopCard），验证 daemon↔progress 接线：
    # 任务完成 → 终态卡编辑 + 另发一条含 "完成" 的新消息（手机通知唯一通道）
    monkeypatch.setattr(progress, "ProgressCard", _REAL_PROGRESS_CARD)
    bundle.d.handle_update(msg_update(text="/new 集成验证任务"))
    assert wait_for(lambda: bundle.store.get(1)["status"] == tasks.STATUS_DONE)
    # 另发的终态通知落在 sent 中（编辑落在 edited 中）
    assert wait_for(lambda: any("完成" in s["text"] for s in bundle.api.sent))
    assert bundle.api.edited, "进度卡应至少编辑过一次"


# ── 附加：/tasks 附 PC 接管命令 + 标题转义 ──────────────────────────────────
def test_tasks_lists_resume_command_and_escapes_title(bundle):
    sid = "7eb0d21b-4eeb-4016-bd88-2527d98620fa"
    bundle.store.create(session_id=sid, title="任务 <opt> 优化",
                        status=tasks.STATUS_DONE)
    bundle.store.create(session_id=None, title="无会话任务",
                        status=tasks.STATUS_QUEUED)
    bundle.d.handle_update(msg_update(text="/tasks"))
    out = bundle.api.all_text()
    assert f"claude --resume {sid}" in out
    assert "<code>claude --resume" in out
    assert "任务 &lt;opt&gt; 优化" in out
    assert "任务 <opt>" not in out
    assert "(无会话 ID，暂不可接管)" in out


# ── 会话锁守卫：PC 端持有该会话时 TG 拒接手 ─────────────────────────────────
def test_converse_refused_when_pc_holds_session(bundle, monkeypatch):
    tid = bundle.store.create(session_id="pc-held-sid", title="路上起的任务",
                              status=tasks.STATUS_DONE, chat_id=str(WHITELIST_UID))
    bundle.d._set_current(str(WHITELIST_UID), tid)
    monkeypatch.setattr(session_lock, "pc_active",
                        lambda sid, **k: sid == "pc-held-sid")
    bundle.d.handle_update(msg_update(text="接着把结论写完"))
    time.sleep(0.15)
    assert bundle.rcalls["resume"] == []
    assert "电脑上打开" in bundle.api.all_text()


def test_say_refused_when_pc_holds_session(bundle, monkeypatch):
    tid = bundle.store.create(session_id="pc-held-2", title="任务",
                              status=tasks.STATUS_DONE, chat_id=str(WHITELIST_UID))
    monkeypatch.setattr(session_lock, "pc_active",
                        lambda sid, **k: sid == "pc-held-2")
    bundle.d.handle_update(msg_update(text=f"/say {tid} 继续"))
    time.sleep(0.15)
    assert bundle.rcalls["resume"] == []
    assert "电脑上打开" in bundle.api.all_text()


# ── 强制接手：软锁拦下后一键越过 ────────────────────────────────────────────
def test_pc_busy_offers_force_button(bundle, monkeypatch):
    monkeypatch.setattr(session_lock, "pc_active", lambda sid, **k: True)
    blocked = bundle.d._pc_busy(str(WHITELIST_UID), "busy-sid")
    assert blocked is True
    kb = bundle.api.sent[-1]["reply_markup"]["inline_keyboard"]
    cbs = [btn["callback_data"] for row in kb for btn in row]
    assert "force:busy-sid" in cbs


def test_force_take_releases_lock_and_switches(bundle, monkeypatch):
    released = []
    monkeypatch.setattr(session_lock, "release_pc", lambda sid: released.append(sid))
    tid = bundle.store.create(session_id="force-sid", title="等待中的会话",
                              status=tasks.STATUS_DONE, chat_id=str(WHITELIST_UID))
    cq = {"callback_query": {"id": "cbf", "from": {"id": WHITELIST_UID},
                             "message": {"chat": {"id": WHITELIST_UID},
                                         "message_id": 888},
                             "data": "force:force-sid"}}
    bundle.d.handle_update(cq)
    assert released == ["force-sid"]
    assert bundle.d._current[str(WHITELIST_UID)] == tid
    assert ("cbf", "已强制接手") in bundle.api.answered


def test_force_take_non_whitelist_dropped(bundle):
    cq = {"callback_query": {"id": "cbx", "from": {"id": 999999},
                             "message": {"chat": {"id": 999999}},
                             "data": "force:whatever"}}
    bundle.d.handle_update(cq)
    assert bundle.d._current == {}


# ── /current 回显当前会话 + resume 命令 + 上文 ──────────────────────────────
def _write_session_multi(dirpath, uuid, mtime, extra_lines):
    p = os.path.join(dirpath, uuid + ".jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"type":"user","message":{"role":"user","content":"起始问题"}}\n')
        for ln in extra_lines:
            f.write(ln + "\n")
    os.utime(p, (mtime, mtime))
    return p


def test_tail_session_picks_last_assistant_text_and_tool(bundle, tmp_path):
    sid = "dddddddd-1111-2222-3333-444444444444"
    lines = [
        '{"type":"assistant","message":{"content":[{"type":"text","text":"早先的回复"}]}}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{}}]}}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"这是最后一条回复"}]}}',
    ]
    p = _write_session_multi(str(tmp_path), sid, time.time(), lines)
    text, tool = bundle.d._tail_session(p)
    assert text == "这是最后一条回复"
    assert tool == "Bash"


def test_current_shows_resume_and_context(bundle, tmp_path, monkeypatch):
    sid = "ffffffff-1111-2222-3333-444444444444"
    sdir = tmp_path / "projects"
    sdir.mkdir()
    _write_session_multi(str(sdir), sid, time.time(),
        ['{"type":"assistant","message":{"content":[{"type":"text","text":"当前进展摘要"}]}}'])
    monkeypatch.setattr(config, "SESSIONS_PROJECT_DIR", str(sdir))
    tid = bundle.store.create(session_id=sid, title="路上起的活",
                              status=tasks.STATUS_DONE, chat_id=str(WHITELIST_UID))
    bundle.d._set_current(str(WHITELIST_UID), tid)
    bundle.d.handle_update(msg_update(text="/current"))
    out = bundle.api.all_text()
    assert f"claude --resume {sid}" in out
    assert "路上起的活" in out
    assert "当前进展摘要" in out


def test_current_no_session_hint(bundle):
    bundle.d.handle_update(msg_update(text="/current"))
    assert "当前没有会话" in bundle.api.all_text()


# ── /watch 观察集 + relay 并入 ──────────────────────────────────────────────
def test_watch_by_task_id_adds_sid(bundle):
    tid = bundle.store.create(session_id="watch-sid-1", title="电脑会话",
                              status=tasks.STATUS_DONE, chat_id=str(WHITELIST_UID))
    bundle.d.handle_update(msg_update(text=f"/watch {tid}"))
    assert "watch-sid-1" in bundle.d.watched_set()
    assert "已观察" in bundle.api.all_text()


def test_unwatch_removes(bundle):
    tid = bundle.store.create(session_id="watch-sid-3", title="x",
                              status=tasks.STATUS_DONE)
    bundle.d.handle_update(msg_update(text=f"/watch {tid}"))
    bundle.d.handle_update(msg_update(text=f"/unwatch {tid}", update_id=2))
    assert "watch-sid-3" not in bundle.d.watched_set()


def test_watch_unknown_token_errors(bundle):
    bundle.d.handle_update(msg_update(text="/watch 999999"))
    assert "未找到" in bundle.api.all_text()
    assert bundle.d.watched_set() == set()


# ── /extend 延长在跑任务硬超时 ──────────────────────────────────────────────
def test_extend_running_task_calls_runner_extend(bundle, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "extend",
                        lambda sid, secs: (calls.append((sid, secs)) or True))
    tid = bundle.store.create(session_id="ext-sid", title="长任务",
                              status=tasks.STATUS_RUNNING, chat_id=str(WHITELIST_UID))
    bundle.d.handle_update(msg_update(text=f"/extend {tid} 3"))
    assert calls == [("ext-sid", 3 * 3600)]


def test_extend_non_running_hint(bundle):
    tid = bundle.store.create(session_id="ext-sid3", title="x",
                              status=tasks.STATUS_DONE)
    bundle.d.handle_update(msg_update(text=f"/extend {tid}"))
    assert "未在运行" in bundle.api.all_text()


def test_attach_origin_gets_longer_timeout(bundle):
    assert bundle.d._task_timeout({"origin": "attach"}) == config.ATTACH_TASK_TIMEOUT
    assert bundle.d._task_timeout({"origin": "new"}) == config.TASK_TIMEOUT


# ── _session_title 优先 AI 标题 ─────────────────────────────────────────────
def test_session_title_prefers_ai_title(bundle, tmp_path):
    p = os.path.join(str(tmp_path), "s.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"type":"user","message":{"role":"user","content":"原始粗糙的第一句"}}\n')
        f.write('{"type":"ai-title","aiTitle":"优化会话机制","sessionId":"s"}\n')
    assert bundle.d._session_title(p) == "优化会话机制"


def test_session_title_fallback_to_user_msg(bundle, tmp_path):
    p = os.path.join(str(tmp_path), "s3.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"type":"user","message":{"role":"user","content":"没有AI标题就用这句"}}\n')
    assert bundle.d._session_title(p) == "没有AI标题就用这句"


# ── /detach 待机：清当前会话指针，下一条普通消息开全新会话 ────────────────────
def test_detach_clears_current_next_text_starts_fresh(bundle):
    tid = bundle.store.create(session_id="old-sid", title="旧会话",
                              status=tasks.STATUS_DONE, chat_id=str(WHITELIST_UID))
    bundle.d._set_current(str(WHITELIST_UID), tid)
    bundle.d.handle_update(msg_update(text="/detach"))
    assert "待机" in bundle.api.all_text()
    assert str(WHITELIST_UID) not in bundle.d._current
    bundle.d.handle_update(msg_update(text="随手问一句", update_id=2))
    assert wait_for(lambda: len(bundle.rcalls["start"]) == 1)
    assert bundle.rcalls["resume"] == []
    assert bundle.rcalls["start"][0]["session_id"] != "old-sid"


def test_detach_when_already_idle(bundle):
    bundle.d.handle_update(msg_update(text="/detach"))
    assert "本就无会话" in bundle.api.all_text()


# ── /watch 无参 → 会话面板点选切换观察 ──────────────────────────────────────
def test_watch_no_arg_shows_panel(bundle, tmp_path, monkeypatch):
    sdir = tmp_path / "projects"
    sdir.mkdir()
    uuid_a = "abcd9999-1111-2222-3333-444444444444"
    _write_session(str(sdir), uuid_a, "要观察的会话", time.time())
    monkeypatch.setattr(config, "SESSIONS_PROJECT_DIR", str(sdir))
    bundle.d.handle_update(msg_update(text="/watch"))
    kb = bundle.api.sent[-1]["reply_markup"]["inline_keyboard"]
    cbs = [btn["callback_data"] for row in kb for btn in row]
    assert f"wsel:{uuid_a}" in cbs
    for c in cbs:
        assert len(c.encode()) < 64


def test_wsel_toggles_watch_on_and_off(bundle, tmp_path, monkeypatch):
    sdir = tmp_path / "projects"
    sdir.mkdir()
    uuid_a = "abcd8888-1111-2222-3333-444444444444"
    _write_session(str(sdir), uuid_a, "点选观察我", time.time())
    monkeypatch.setattr(config, "SESSIONS_PROJECT_DIR", str(sdir))

    def tap():
        bundle.d.handle_update({"callback_query": {
            "id": "cbw", "from": {"id": WHITELIST_UID},
            "message": {"chat": {"id": WHITELIST_UID}, "message_id": 777},
            "data": f"wsel:{uuid_a}"}})

    tap()
    assert uuid_a in bundle.d.watched_set()
    assert bundle.api.edited, "面板应就地编辑刷新（可连续多选）"
    tap()
    assert uuid_a not in bundle.d.watched_set()


# ── 旁听流：watch 会话的正文实时推手机（thinking/tool 除外）──────────────────
def _watch_setup(bundle, tmp_path, monkeypatch, sid="wtch1111-1111-2222-3333-444444444444"):
    sdir = tmp_path / "projects"
    sdir.mkdir(exist_ok=True)
    p = _write_session(str(sdir), sid, "被旁听的会话", time.time())
    monkeypatch.setattr(config, "SESSIONS_PROJECT_DIR", str(sdir))
    monkeypatch.setattr(config, "CHAT_ID", str(WHITELIST_UID))
    with bundle.d._watched_lock:
        bundle.d._watched.add(sid)
    return sid, p


def test_watch_stream_pushes_new_text_only(bundle, tmp_path, monkeypatch):
    sid, p = _watch_setup(bundle, tmp_path, monkeypatch)
    bundle.d._watch_tick()
    n0 = len(bundle.api.sent)
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"type":"assistant","message":{"content":['
                '{"type":"thinking","thinking":"内心戏不该推"},'
                '{"type":"tool_use","name":"Bash","input":{"command":"secret"}},'
                '{"type":"text","text":"这句正文应该推到手机"}]}}\n')
    bundle.d._watch_tick()
    out = "\n".join(s["text"] for s in bundle.api.sent[n0:])
    assert "这句正文应该推到手机" in out
    assert "内心戏" not in out and "secret" not in out
    n1 = len(bundle.api.sent)
    bundle.d._watch_tick()
    assert len(bundle.api.sent) == n1


def test_watch_stream_skips_active_tg_session(bundle, tmp_path, monkeypatch):
    sid, p = _watch_setup(bundle, tmp_path, monkeypatch)
    bundle.d._watch_tick()
    bundle.store.create(session_id=sid, title="tg任务",
                        status=tasks.STATUS_RUNNING)
    n0 = len(bundle.api.sent)
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"不应双推"}]}}\n')
    bundle.d._watch_tick()
    assert all("不应双推" not in s["text"] for s in bundle.api.sent[n0:])


def test_watch_stream_holds_partial_line(bundle, tmp_path, monkeypatch):
    sid, p = _watch_setup(bundle, tmp_path, monkeypatch)
    bundle.d._watch_tick()
    n0 = len(bundle.api.sent)
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"半行')
    bundle.d._watch_tick()
    assert len(bundle.api.sent) == n0
    with open(p, "a", encoding="utf-8") as f:
        f.write('未完待续"}]}}\n')
    bundle.d._watch_tick()
    assert any("半行未完待续" in s["text"] for s in bundle.api.sent[n0:])


def test_watch_stream_unwatch_stops_and_cleans_offset(bundle, tmp_path, monkeypatch):
    sid, p = _watch_setup(bundle, tmp_path, monkeypatch)
    bundle.d._watch_tick()
    assert sid in bundle.d._watch_offsets
    with bundle.d._watched_lock:
        bundle.d._watched.discard(sid)
    bundle.d._watch_tick()
    assert sid not in bundle.d._watch_offsets
