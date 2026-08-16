#!/usr/bin/env python3
"""初始化知识图谱模式：连接验证 + 六类唯一约束与索引（可重复执行，规范 §13）。

用法：python scripts/init_graph.py
退出码：0 成功；1 连接或模式初始化失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许以 `python scripts/xxx.py` 方式直接运行（未安装包时也能找到 src）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from welding_kg.neo4j_store import Neo4jStore  # noqa: E402
from welding_kg.settings import Settings  # noqa: E402


def main() -> int:
    """执行连接验证与模式初始化，成功返回 0。"""
    settings = Settings.from_env()
    store = Neo4jStore(settings)
    try:
        print(f"[1/2] 验证连接：{settings.neo4j_uri} ...")
        store.verify_connectivity()
        print("      连接成功")
        print("[2/2] 初始化六类唯一约束与 Case 检索字段索引 ...")
        store.initialize_schema()
        print("      模式初始化完成（可重复执行）")
        return 0
    except Exception as exc:  # noqa: BLE001 顶层统一转退出码
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
