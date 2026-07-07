#!/usr/bin/env python3
# config.py — 配置与常量（token / 白名单 / 参数 / 日志脱敏）
#
# 配置来源优先级：环境变量 > ~/.config 下的 config.json。单一真源，daemon 只读不散落。

import json
import os
import re

# ── 状态/配置目录 ──────────────────────────────────────────────────────────
STATE_DIR = os.path.expanduser(
    os.environ.get("TGLR_STATE_DIR", "~/.claude-telegram-longrange"))
CONFIG_JSON = os.path.join(STATE_DIR, "config.json")
OFFSET_FILE = os.path.join(STATE_DIR, "offset")
CLI_VERSION_FILE = os.path.join(STATE_DIR, "cli_version")


def _load_conf():
    if os.path.exists(CONFIG_JSON):
        try:
            with open(CONFIG_JSON, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}
    return {}


_CONF = _load_conf()


def _get(key, env, default=None):
    # 环境变量优先，其次 config.json，最后默认值
    if env in os.environ:
        return os.environ[env]
    return _CONF.get(key, default)


# ── 工作区（claude 子进程的 cwd；resume 会话文件按此 cwd 落盘）──────────────
WORKDIR = _get("workdir", "TGLR_WORKDIR", os.getcwd())

# ── bot token / owner chat ─────────────────────────────────────────────────
BOT_TOKEN = _get("bot_token", "TGLR_BOT_TOKEN")
CHAT_ID = _get("chat_id", "TGLR_CHAT_ID")


def _parse_ids(raw):
    # 支持逗号分隔字符串或列表；元素转 int
    if raw is None:
        return set()
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    out = set()
    for it in items:
        it = str(it).strip()
        if it.isdigit():
            out.add(int(it))
    return out


# ── 鉴权白名单（单一真源，message.from.id 与 callback_query.from.id 共用）──
# 校验放在所有路由之前；callback_query 亦复用此集合，否则拿到审批按钮的
# 任意账号都能批准工具执行（最危险的攻击面）。CHAT_ID 默认并入白名单。
ALLOWED_USER_IDS = _parse_ids(_get("allowed_user_ids", "TGLR_ALLOWED_USER_IDS"))
if CHAT_ID and str(CHAT_ID).strip().lstrip("-").isdigit():
    ALLOWED_USER_IDS.add(int(CHAT_ID))

# ── 模型枚举白名单（/new -m 的取值必须先过此集合才进 argv）──
_models = _get("allowed_models", "TGLR_ALLOWED_MODELS", ["opus", "sonnet", "haiku"])
if isinstance(_models, str):
    _models = [m.strip() for m in _models.split(",") if m.strip()]
ALLOWED_MODELS = set(_models or [])

# ── 并发 / 超时 / 节流 ─────────────────────────────────────────────────────
MAX_CONCURRENCY = int(_get("max_concurrency", "TGLR_MAX_CONCURRENCY", 2))
TASK_TIMEOUT = int(_get("task_timeout", "TGLR_TASK_TIMEOUT", 7200))   # 硬超时 2h
# attach 接管的会话多为"电脑上起的长会话"，放宽到 4h；仍有硬顶防额度失控
ATTACH_TASK_TIMEOUT = int(_get("attach_task_timeout", "TGLR_ATTACH_TASK_TIMEOUT", 14400))
PROGRESS_THROTTLE = int(_get("progress_throttle", "TGLR_PROGRESS_THROTTLE", 20))
TG_MAX = 4000                          # Telegram 单条消息字符上限，超长分段

# ── 审批中继：抗抖动 + 降级回涌护栏 ────────────────────────────────────────
# agents-island 桥正常可达；单次 GET 超时/抖动不代表桥宕。若一抖动就切文件降级，
# 而文件降级又靠"无响应文件=待审批"推断——但 hook 消费后会删响应文件、队列又只留
# 最近若干行——就会把整段历史已决审批当新卡刷屏。故：
#   ① 连续 miss 达阈值才降级（单/双次抖动只跳过本轮，留在权威桥路径）；
#   ② 即便降级，也只认"年龄 ≤ 窗口"的队列条目（超窗口者 hook 早已 defer，非活跃）。
BRIDGE_DEGRADE_AFTER = int(_get("bridge_degrade_after", "TGLR_BRIDGE_DEGRADE_AFTER", 3))
# 降级 pending 最大存活(秒)：取移动端 hook 等待窗口 + 宽限（见 agents-island hook 的
# ISLAND_HOOK_TIMEOUT，长任务常注入 600）。
DEGRADED_PENDING_MAX_AGE = int(
    _get("degraded_pending_max_age", "TGLR_DEGRADED_PENDING_MAX_AGE", 660))


# ── /sessions 只扫本工作区对应的 Claude Code 会话目录 ──
# Claude Code 会话按 cwd 落 ~/.claude/projects/<cwd 转义>/，转义规则：非字母数字转 '-'。
def _project_slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


SESSIONS_PROJECT_DIR = os.path.expanduser(
    os.path.join("~/.claude/projects", _project_slug(WORKDIR)))


# ── 日志脱敏 ────────────────────────────────────────────────────────────────
# 匹配 bot<数字>:<字母数字_-> 形态（api.telegram.org/bot<token> 与 /file/bot<token>/ 皆命中）
_TOKEN_RE = re.compile(r"bot\d+:[\w-]+")


def redact(text):
    """把任意文本中的 bot<token> 段替换为 bot***。
    所有进日志的字符串（尤其 requests 异常 repr）必须先过此函数，否则 token 泄进日志。"""
    if text is None:
        return ""
    return _TOKEN_RE.sub("bot***", str(text))


# ── Markdown → Telegram HTML ────────────────────────────────────────────────
# Claude 回复是 Markdown；用 parse_mode=HTML 发送，故必须转换，否则：
#   ① `**加粗**` 原样显示成一堆星号；
#   ② 原文里的 < & > 会被 Telegram 当标签解析 → 400 错误 → 消息静默发不出。
_MD_CODEBLOCK = re.compile(r"```[\w-]*\n?(.*?)```", re.S)
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_html(text):
    """把 Claude 的 Markdown 转成 Telegram 支持的 HTML 子集（b/code/pre）。
    先抽出代码块占位，转义全文，再回填，最后处理行内加粗/代码——
    确保任何 < & > 都被转义，杜绝 parse_mode=HTML 解析失败。"""
    if not text:
        return ""
    blocks = []

    def _stash(m):
        blocks.append(m.group(1))
        return f"\x00B{len(blocks) - 1}\x00"

    t = _MD_CODEBLOCK.sub(_stash, str(text))
    inlines = []

    def _stashi(m):
        inlines.append(m.group(1))
        return f"\x00I{len(inlines) - 1}\x00"

    t = _MD_INLINE_CODE.sub(_stashi, t)
    t = _esc(t)                                   # 转义正文（此时代码已抽走）
    t = _MD_BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", t)
    for i, code in enumerate(inlines):
        t = t.replace(f"\x00I{i}\x00", f"<code>{_esc(code)}</code>")
    for i, code in enumerate(blocks):
        t = t.replace(f"\x00B{i}\x00", f"<pre>{_esc(code)}</pre>")
    return t
