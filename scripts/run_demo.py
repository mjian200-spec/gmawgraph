#!/usr/bin/env python3
"""端到端推理演示（规范 §13 + 可读化报告）。

用法：
  python scripts/run_demo.py examples/requirement.json           # 默认：易读中文报告
  python scripts/run_demo.py examples/requirement.json --json    # 原始 UTF-8 JSON（规范 §13 契约）

退出码：0 成功；1 输入、校验或连接错误。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import ValidationError  # noqa: E402

from welding_kg.demo_renderer import format_demo_result  # noqa: E402
from welding_kg.models import WeldingRequirement  # noqa: E402
from welding_kg.service import run_reasoning_demo  # noqa: E402


def _strip_embeddings(obj):
    """递归删除输出中的 embedding 大向量，保证 Demo JSON 可读。"""
    if isinstance(obj, dict):
        return {k: _strip_embeddings(v) for k, v in obj.items() if k != "embedding"}
    if isinstance(obj, list):
        return [_strip_embeddings(v) for v in obj]
    return obj


def main() -> int:
    """读取需求文件 → 模型校验 → 运行推理演示 → 按模式输出。"""
    args = [a for a in sys.argv[1:]]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]

    if len(args) < 1:
        print("用法：python scripts/run_demo.py <requirement.json> [--json]", file=sys.stderr)
        return 1
    path = Path(args[0])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        requirement = WeldingRequirement.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"需求文件读取或校验失败：{exc}", file=sys.stderr)
        return 1

    # top_k 可通过环境变量 DEMO_TOP_K 覆盖（默认 5，规范 §12）
    top_k = int(__import__("os").getenv("DEMO_TOP_K", "5"))
    result = run_reasoning_demo(requirement, top_k=top_k)

    if as_json:
        # 原始 JSON 输出（规范 §13 契约）
        print(json.dumps(_strip_embeddings(result.model_dump()), ensure_ascii=False, indent=2))
    else:
        # 默认输出易读中文报告
        print(format_demo_result(requirement, result))

    # 无基准案例视为演示未完成（连接错误等由异常向上抛）
    if result.base_case is None:
        print("演示无结果（见 warnings 字段）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
