import os
import sys

import pytest

# 让测试从 src/ 导入模块（config/daemon/runner/tasks/progress/approval_relay/session_lock）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import session_lock  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_session_lock_dir(tmp_path, monkeypatch):
    # 任何测试都绝不写真实锁目录（daemon 用同目录）：统一把会话锁目录指向 tmp。
    # runner._run 会无条件 acquire_tg，凡直接跑 _run 的测试都会落盘，故在此根治。
    monkeypatch.setattr(session_lock, "LOCK_DIR", str(tmp_path / "sl_locks"))
    yield
