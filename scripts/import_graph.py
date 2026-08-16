#!/usr/bin/env python3
"""导入图谱规则 JSON（规范 §13）。

用法：python scripts/import_graph.py [graph_seed.json]
默认文件：data/seed/graph_seed.json
退出码：0 成功；1 输入、校验或连接错误。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from welding_kg.graph_importer import import_graph_json  # noqa: E402
from welding_kg.neo4j_store import Neo4jStore  # noqa: E402
from welding_kg.settings import Settings  # noqa: E402


def main() -> int:
    """解析参数并执行导入，打印统计结果。"""
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else str(Path(__file__).resolve().parents[1] / "data" / "seed" / "graph_seed.json")
    )
    if not Path(path).exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 1

    store = Neo4jStore(Settings.from_env())
    try:
        result = import_graph_json(path, store)
    finally:
        store.close()

    print(f"导入完成：{result.file}")
    print(f"  新增 {result.created}，更新 {result.updated}，跳过 {result.skipped}")
    for w in result.warnings:
        print(f"  [警告] {w}")
    for e in result.errors:
        print(f"  [错误] {e}", file=sys.stderr)
    return 0 if not result.errors else 1


if __name__ == "__main__":
    sys.exit(main())
