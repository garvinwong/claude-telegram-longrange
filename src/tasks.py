#!/usr/bin/env python3
# tasks.py — tg-longrange 任务台账（SQLite）
#
# 设计要点：
# 台账是 daemon 崩溃/WSL 重启后恢复任务追踪的唯一真源；进程存活信息（pid/pgid）
# 与会话身份（session_id）必须落盘，否则孤儿 claude 进程无法回收、无法接力。

import os
import sqlite3
import threading
from datetime import datetime, timezone

# 状态枚举——单一真源，越界状态视为脏数据
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_WAITING_APPROVAL = "waiting_approval"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

# active = 仍会被 relay 关注、仍可能产生审批推送的状态；供该阶段 过滤本机开发会话
_ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_WAITING_APPROVAL)

DEFAULT_DB_PATH = os.path.expanduser(
    os.path.join(os.environ.get("TGLR_STATE_DIR", "~/.claude-telegram-longrange"),
                 "tasks.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    title TEXT,
    model TEXT,
    origin TEXT,            -- new | attach
    status TEXT,            -- 见上方枚举
    pid INTEGER,
    pgid INTEGER,
    progress_msg_id INTEGER,
    chat_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    last_event TEXT,
    deadline TEXT
);
"""

# update_fields 白名单——只允许改这些列，杜绝调用方拼列名注入 SQL（S2 防呆）
_UPDATABLE_COLUMNS = frozenset({
    "session_id", "title", "model", "origin", "status", "pid", "pgid",
    "progress_msg_id", "chat_id", "last_event", "deadline",
})


def _utcnow():
    # 约束：时间戳统一 UTC ISO8601，跨机（WSL 本机/GCP）比较不受本地时区干扰
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """任务台账。

    并发模型：daemon 主循环、relay 线程、runner 线程会并发访问同一台账。
    这里选用「单连接 + 全局 Lock」：SQLite 单连接跨线程需 check_same_thread=False，
    并用 threading.Lock 串行化所有读写——台账写频率低（任务级事件），Lock 争用可忽略，
    换来的是不必担心多连接下 WAL/锁升级的竞态，实现更简单可控。
    """

    def __init__(self, db_path=DEFAULT_DB_PATH):
        self._db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        # 约束：check_same_thread=False 是跨线程共享单连接的前提，配合 self._lock 使用
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ── CRUD ────────────────────────────────────────────────────────────
    def create(self, session_id=None, title=None, model=None, origin="new",
               status=STATUS_QUEUED, chat_id=None, pid=None, pgid=None,
               progress_msg_id=None, deadline=None):
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO tasks
                   (session_id, title, model, origin, status, pid, pgid,
                    progress_msg_id, chat_id, created_at, updated_at,
                    last_event, deadline)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, title, model, origin, status, pid, pgid,
                 progress_msg_id, chat_id, now, now, None, deadline),
            )
            self._conn.commit()
            return cur.lastrowid

    def get(self, task_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_by_session(self, session_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE session_id=? ORDER BY task_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_status(self, task_id, status, last_event=None):
        # last_event 可选：完成/失败等关键迁移常伴一句人读说明，一并落盘便于进度卡展示
        fields = {"status": status}
        if last_event is not None:
            fields["last_event"] = last_event
        return self.update_fields(task_id, **fields)

    def update_fields(self, task_id, **fields):
        if not fields:
            return 0
        bad = set(fields) - _UPDATABLE_COLUMNS
        if bad:
            # 约束：拒绝白名单外列名，防止调用方经 **kwargs 触及非预期列或注入
            raise ValueError(f"不可更新的字段: {sorted(bad)}")
        cols = list(fields)
        assignments = ", ".join(f"{c}=?" for c in cols)
        values = [fields[c] for c in cols]
        values.append(_utcnow())   # updated_at 每次写入自动刷新
        values.append(task_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET {assignments}, updated_at=? WHERE task_id=?",
                values,
            )
            self._conn.commit()
            return cur.rowcount

    def list_recent(self, limit=20):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY task_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def active_session_ids(self):
        # 供该阶段 relay 过滤：只有台账内活跃态任务的会话审批才推 TG，
        # 本机终端开发会话不在台账 → 不推送
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT session_id FROM tasks "
                f"WHERE status IN ({placeholders}) AND session_id IS NOT NULL",
                _ACTIVE_STATUSES,
            ).fetchall()
        return {r["session_id"] for r in rows}

    # ── 启动恢复扫描 ─────────────────────────────────────────────────────
    def recover_on_start(self):
        """daemon 启动时调用，修正上次异常退出遗留的 running 态。

        判定规则：
          - status=running 且 PID 已死（os.kill(pid,0) 抛异常）→ interrupted：
            进程没了，输出流也断了，任务无法续跑，标记中断供 /say 接力。
          - status=running 且 PID 仍活 → 保持 running，但因 daemon 重启后
            stdout 管道已断、无法重新 attach 事件流（R3 简化处理），
            在 last_event 标注 orphaned，由上层决定 killpg 或放任其跑完后 resume 查看。

        返回 {"interrupted": [task_id...], "orphaned": [task_id...]}。
        """
        result = {"interrupted": [], "orphaned": []}
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, pid FROM tasks WHERE status=?", (STATUS_RUNNING,)
            ).fetchall()
            now = _utcnow()
            for r in rows:
                tid, pid = r["task_id"], r["pid"]
                alive = self._pid_alive(pid)
                if alive:
                    self._conn.execute(
                        "UPDATE tasks SET last_event=?, updated_at=? WHERE task_id=?",
                        ("orphaned", now, tid),
                    )
                    result["orphaned"].append(tid)
                else:
                    self._conn.execute(
                        "UPDATE tasks SET status=?, last_event=?, updated_at=? WHERE task_id=?",
                        (STATUS_INTERRUPTED, "daemon 重启时 PID 已死", now, tid),
                    )
                    result["interrupted"].append(tid)
            self._conn.commit()
        return result

    @staticmethod
    def _pid_alive(pid):
        # 约束：pid 缺失/非法一律视为已死，避免误判为活进程而放任孤儿
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # 进程存在但不属本用户——WSL 单用户场景基本不会出现，保守视为存活
            return True
        except OSError:
            return False
        return True
