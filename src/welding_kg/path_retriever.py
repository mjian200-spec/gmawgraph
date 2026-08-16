"""多跳路径查询层（规范 §9 路径查询层、§11 固定 Cypher）。

固定查询链：Condition → Parameter → Mechanism → [Mechanism 0-2 跳] → Quality。
首段关系为 SUGGESTS_ADJUSTMENT，其余为 AFFECTS/DETERMINES，
总路径不超过 6 条关系；路径查询使用固定 Cypher，不执行 LLM 生成的 Cypher。
"""

from __future__ import annotations

from .models import (
    DifferenceItem,
    PathNode,
    PathRelation,
    ReasoningPath,
)
from .neo4j_store import Neo4jStore

# 路径上节点与关系的返回投影（三段查询共用）
_NODE_PROJECTION = (
    "{label: head(labels(n)), key: coalesce(n.code, n.equipment_id, ''), "
    "name: n.name, definition: n.definition}"
)
_REL_PROJECTION = (
    "{type: type(r), source_change: r.source_change, target_change: r.target_change, "
    "condition_text: r.condition_text, confidence: r.confidence, "
    "provenance_type: r.provenance_type, "
    "source_refs: coalesce(r.source_refs, [])}"
)

# 固定模板：机理内部 0 / 1 / 2 跳三种模式（避免可变长度匹配越界，规范 §9）
_HOP_PATTERNS = {
    0: (
        "MATCH p = (cond:Condition {code: $code})-[:SUGGESTS_ADJUSTMENT]->"
        "(pa:Parameter)-[:AFFECTS]->(m:Mechanism)-[:AFFECTS]->(q:Quality)"
    ),
    1: (
        "MATCH p = (cond:Condition {code: $code})-[:SUGGESTS_ADJUSTMENT]->"
        "(pa:Parameter)-[:AFFECTS]->(m:Mechanism)-[:AFFECTS|DETERMINES]->"
        "(m2:Mechanism)-[:AFFECTS]->(q:Quality)"
    ),
    2: (
        "MATCH p = (cond:Condition {code: $code})-[:SUGGESTS_ADJUSTMENT]->"
        "(pa:Parameter)-[:AFFECTS]->(m:Mechanism)-[:AFFECTS|DETERMINES]->"
        "(m2:Mechanism)-[:AFFECTS|DETERMINES]->(m3:Mechanism)-[:AFFECTS]->(q:Quality)"
    ),
}


def _node_sequence_key(path_nodes: list[PathNode]) -> tuple[str, ...]:
    """路径节点序列的稳定键（去重用）。"""
    return tuple(n.key for n in path_nodes)


def _query_paths_for_code(
    store: Neo4jStore, code: str, quality_filter: str | None, limit: int
) -> list[dict]:
    """按单个差异代码查询固定模板路径，返回原始记录列表。"""
    raw_paths: list[dict] = []
    for hops, pattern in _HOP_PATTERNS.items():
        query = (
            pattern
            + f"\nWHERE $quality IS NULL OR q.code = $quality"
            f"\nRETURN [n IN nodes(p) | {_NODE_PROJECTION}] AS nodes,"
            f" [r IN relationships(p) | {_REL_PROJECTION}] AS rels"
            f"\nLIMIT $limit"
        )
        rows = store.execute_read(
            query, {"code": code, "quality": quality_filter, "limit": limit * 4}
        )
        raw_paths.extend(rows)
    return raw_paths


def _query_equipment_limits(
    store: Neo4jStore, parameter_codes: set[str]
) -> dict[str, list[dict]]:
    """查询设备对参数的约束，按参数 code 分组返回。"""
    if not parameter_codes:
        return {}
    rows = store.execute_read(
        "MATCH (e:Equipment)-[r:LIMITS]->(pa:Parameter) "
        "WHERE pa.code IN $codes "
        "RETURN pa.code AS code, e.equipment_id AS equipment_id, properties(r) AS props",
        {"codes": sorted(parameter_codes)},
    )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["code"], []).append(
            {
                "equipment_id": row["equipment_id"],
                "properties": row["props"],
            }
        )
    return grouped


def query_reasoning_paths(
    differences: list[DifferenceItem],
    store: Neo4jStore,
    target_quality: str | None = None,
    limit_per_difference: int = 5,
) -> list[ReasoningPath]:
    """按差异代码查询候选推理路径（规范 §9）。

    按差异代码、终点质量和节点序列去重，合并关系的 source_refs；
    另行查询 Equipment-LIMITS→Parameter 并附加到路径结果。
    """
    paths: list[ReasoningPath] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()  # 去重键
    path_index = 0

    for diff in differences:
        raw_paths = _query_paths_for_code(
            store, diff.code, target_quality, limit_per_difference
        )
        kept_for_diff = 0
        for raw in raw_paths:
            nodes = [PathNode(**n) for n in raw["nodes"]]
            relations = [PathRelation(**r) for r in raw["rels"]]
            # 终点质量取最后一个节点
            end_quality = nodes[-1].key if nodes else ""
            seq_key = _node_sequence_key(nodes)
            # 按 差异代码+终点质量+节点序列 去重（规范 §9）
            dedup_key = (diff.code, end_quality, seq_key)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            if kept_for_diff >= limit_per_difference:
                break
            kept_for_diff += 1

            # 合并整条路径上关系的 source_refs 并去重
            merged_refs: list[str] = []
            for rel in relations:
                for ref in rel.source_refs:
                    if ref not in merged_refs:
                        merged_refs.append(ref)

            # 附加设备限制（Equipment-LIMITS→Parameter，规范 §9）
            param_codes = {n.key for n in nodes if n.label == "Parameter"}
            equipment_limits: list[dict] = []
            for code, limits in _query_equipment_limits(store, param_codes).items():
                equipment_limits.extend(limits)

            paths.append(
                ReasoningPath(
                    path_id=f"p{path_index:03d}",
                    difference_code=diff.code,
                    nodes=nodes,
                    relations=relations,
                    source_refs=merged_refs,
                    equipment_limits=equipment_limits,
                )
            )
            path_index += 1
    return paths
