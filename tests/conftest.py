import os
import sys

# 让测试从 src/ 导入模块（config/daemon/runner/tasks/progress/approval_relay）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
