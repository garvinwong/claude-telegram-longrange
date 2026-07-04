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
PROGRESS_THROTTLE = int(_get("progress_throttle", "TGLR_PROGRESS_THROTTLE", 20))
TG_MAX = 4000                          # Telegram 单条消息字符上限，超长分段


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
