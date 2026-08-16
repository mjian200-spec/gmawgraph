"""pytest 公共配置。

测试分组（验收规范 P1）：
- 默认运行（不含 integration 标记）：纯函数与 FakeStore 单元测试，
  无外部服务也必须全部通过；
- `-m integration`：真实 Neo4j/BGE/LLM 集成测试，正式验收时不得跳过
  （Neo4j 不可达直接失败，见 tests/test_adjustment_integration.py）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
