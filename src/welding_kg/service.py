"""服务入口（规范 §12）。

执行顺序：检索案例 → 选最高分案例 → 计算差异 → 查询路径 → LLM 选择路径。
没有案例或路径时返回空列表和 warning，不抛出不可读错误。
"""

from __future__ import annotations

from .case_comparator import compare_case
from .case_retriever import find_similar_cases
from .models import DemoResult, WeldingRequirement
from .neo4j_store import Neo4jStore
from .path_planner import select_reasoning_paths
from .path_retriever import query_reasoning_paths
from .settings import Settings, load_retrieval_config


def run_reasoning_demo(
    requirement: WeldingRequirement, top_k: int = 5
) -> DemoResult:
    """端到端推理演示：需求 → 最相似案例 → 差异 → 多跳路径 → LLM 选择（规范 §12）。"""
    settings = Settings.from_env()
    cfg = load_retrieval_config()
    warnings: list[str] = []
    store = Neo4jStore(settings)
    try:
        # 1) 检索最相似案例
        matches = find_similar_cases(requirement, store, top_k=top_k)
        if not matches:
            warnings.append(
                f"未找到工艺为 {requirement.process!r} 的历史案例，请先导入 cases.json"
            )
            return DemoResult(
                case_matches=[],
                base_case=None,
                differences=[],
                candidate_paths=[],
                selected_path_ids=[],
                selection_reason="",
                warnings=warnings,
            )
        # 汇总检索层 warning（如嵌入服务退化）
        for w in matches[0].warnings:
            if w not in warnings:
                warnings.append(w)

        # 2) 选最高分案例作为基准
        base_case = matches[0].case

        # 3) 计算需求与基准案例的差异
        differences = compare_case(requirement, base_case)

        # 4) 查询候选推理路径
        candidate_paths = query_reasoning_paths(
            differences,
            store,
            target_quality=requirement.target_quality,
            limit_per_difference=cfg.get("limit_per_difference", 5),
        )
        if not candidate_paths:
            warnings.append("未查询到与差异匹配的推理路径（图规则可能未覆盖这些差异代码）")

        # 5) LLM 路径选择
        selection = select_reasoning_paths(
            requirement, base_case, differences, candidate_paths
        )
        warnings.extend(selection.warnings)

        return DemoResult(
            case_matches=matches,
            base_case=base_case,
            differences=differences,
            candidate_paths=candidate_paths,
            selected_path_ids=selection.selected_path_ids,
            selection_reason=selection.selection_reason,
            warnings=warnings,
        )
    finally:
        store.close()
