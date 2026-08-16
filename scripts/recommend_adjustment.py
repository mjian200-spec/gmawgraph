#!/usr/bin/env python3
"""焊接参数修正量生成 CLI（adjustment_generation_spec §15）。

用法：
  python scripts/recommend_adjustment.py \
    --requirement data/example_requirement.json \
    --equipment crobotpos_arc_module \
    --top-k 5 --proposal-count 3

输出：UTF-8 JSON（requirement_id, equipment_id, proposals,
selected_proposal_id, warnings；规范 §15）。
约束（验收规范 P1）：1 <= top-k <= 5、1 <= proposal-count <= 3，
非法取值返回非零退出码与清晰错误。
退出码：0 成功；1 输入、校验或连接错误；2 命令行参数错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import ValidationError  # noqa: E402

from welding_kg.adjustment import generate_adjustment_recommendations_sync  # noqa: E402
from welding_kg.models import AdjustmentResult, WeldingRequirement  # noqa: E402


def _bounded_int(low: int, high: int, name: str):
    """构造范围受限的整数参数解析器（验收规范 P1）。"""

    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} 必须是整数：{value!r}")
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(
                f"{name} 必须在 [{low}, {high}]，当前为 {number}"
            )
        return number

    return parse


def main() -> int:
    """读取需求与设备参数，生成修正方案并输出 JSON。"""
    parser = argparse.ArgumentParser(description="焊接参数修正量生成（规范 §15）")
    parser.add_argument("--requirement", required=True, help="需求 JSON 文件路径")
    parser.add_argument("--equipment", required=True, help="设备 equipment_id")
    parser.add_argument(
        "--top-k", type=_bounded_int(1, 5, "--top-k"), default=5,
        help="相似案例检索数量，1—5（默认 5）",
    )
    parser.add_argument(
        "--proposal-count", type=_bounded_int(1, 3, "--proposal-count"), default=3,
        help="基准案例方案数，1—3（默认 3）",
    )
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM 路径选择与摘要（使用程序化摘要）")
    args = parser.parse_args()

    path = Path(args.requirement)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        requirement = WeldingRequirement.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"需求文件读取或校验失败：{exc}", file=sys.stderr)
        return 1

    warnings: list[str] = []
    try:
        proposals = generate_adjustment_recommendations_sync(
            requirement,
            equipment_id=args.equipment,
            top_k=args.top_k,
            proposal_count=args.proposal_count,
            use_llm=not args.no_llm,
            warnings=warnings,
        )
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    # 方案级告警合并到全局告警（规范 §15 warnings）
    for proposal in proposals:
        for warn in proposal.warnings:
            if warn not in warnings:
                warnings.append(warn)

    # 排序后的首项即最高分方案（验收规范 P1）
    selected = proposals[0].proposal_id if proposals else None

    result = AdjustmentResult(
        requirement_id=requirement.requirement_id or "anonymous_requirement",
        equipment_id=args.equipment,
        proposals=proposals,
        selected_proposal_id=selected,
        warnings=warnings,
    )
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    return 0 if proposals else 1


if __name__ == "__main__":
    sys.exit(main())
