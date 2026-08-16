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

# 机理尾部模板：Parameter → Mechanism → [Mechanism 0—2 跳] → Quality。
# 同时供差异路径查询（Condition-SUGGESTS_ADJUSTMENT→Parameter + 尾部）与
# 修正量推理链查询（Parameter + 尾部，adjustment_generation_spec §8）复用，
# 避免两份固定 Cypher 模板漂移。
_MECHANISM_TAILS = {
    0: (
        "-[r1:AFFECTS]->(m:Mechanism)-[r2:AFFECTS]->(q:Quality)",
        ["r1", "r2"],
    ),
    1: (
        "-[r1:AFFECTS]->(m:Mechanism)-[r2:AFFECTS|DETERMINES]->"
        "(m2:Mechanism)-[r3:AFFECTS]->(q:Quality)",
        ["r1", "r2", "r3"],
    ),
    2: (
        "-[r1:AFFECTS]->(m:Mechanism)-[r2:AFFECTS|DETERMINES]->"
        "(m2:Mechanism)-[r3:AFFECTS|DETERMINES]->"
        "(m3:Mechanism)-[r4:AFFECTS]->(q:Quality)",
        ["r1", "r2", "r3", "r4"],
    ),
}

# 固定模板：机理内部 0 / 1 / 2 跳三种模式（避免可变长度匹配越界，规范 §9）
_HOP_PATTERNS = {
    hops: (
        "MATCH p = (cond:Condition {code: $code})-[:SUGGESTS_ADJUSTMENT]->"
        f"(pa:Parameter){tail}"
    )
    for hops, (tail, _aliases) in _MECHANISM_TAILS.items()
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


def query_equipment_limits(
    store: Neo4jStore,
    parameter_codes: list[str] | None,
    equipment_id: str | None = None,
) -> dict[str, list[dict]]:
    """查询设备对参数的约束，按参数 code 分组返回。

    equipment_id 给定时只查该设备的 LIMITS；None 时查全部设备
    （adjustment_generation_spec §8/§10）。
    parameter_codes 为 None 时不按参数过滤。
    """
    rows = store.execute_read(
        "MATCH (e:Equipment)-[r:LIMITS]->(pa:Parameter) "
        "WHERE ($eid IS NULL OR e.equipment_id = $eid) "
        "AND ($codes IS NULL OR pa.code IN $codes) "
        "RETURN pa.code AS code, e.equipment_id AS equipment_id, properties(r) AS props",
        {
            "eid": equipment_id,
            "codes": sorted(parameter_codes) if parameter_codes else None,
        },
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


def get_equipment_limits(
    equipment_id: str,
    parameter_codes: list[str] | None,
    store: Neo4jStore,
) -> list[dict]:
    """设备对参数的 LIMITS 关系（扁平行，adjustment_generation_spec §15）。

    返回行：{equipment_id, parameter_code, properties}；
    parameter_codes 为 None 时返回该设备全部参数限制。
    """
    grouped = query_equipment_limits(store, parameter_codes, equipment_id)
    rows: list[dict] = []
    for code in sorted(grouped):
        for entry in grouped[code]:
            rows.append(
                {
                    "equipment_id": entry["equipment_id"],
                    "parameter_code": code,
                    "properties": entry["properties"],
                }
            )
    return rows


def get_adjustment_paths(
    condition_codes: list[str],
    parameter_codes: list[str],
    store: Neo4jStore,
) -> list[dict]:
    """查询 Condition-SUGGESTS_ADJUSTMENT→Parameter 调整规则
    （adjustment_generation_spec §15）。

    返回行：{id, condition_code, parameter_code, target_change, confidence,
    condition_text, provenance_type, source_refs}。
    """
    if not condition_codes or not parameter_codes:
        return []
    return store.execute_read(
        "MATCH (c:Condition)-[r:SUGGESTS_ADJUSTMENT]->(pa:Parameter) "
        "WHERE c.code IN $cc AND pa.code IN $pc "
        "RETURN r.id AS id, c.code AS condition_code, pa.code AS parameter_code, "
        "r.target_change AS target_change, r.confidence AS confidence, "
        "r.condition_text AS condition_text, r.provenance_type AS provenance_type, "
        "coalesce(r.source_refs, []) AS source_refs",
        {"cc": sorted(condition_codes), "pc": sorted(parameter_codes)},
    )


def get_path_sources(path_ids: list[str], store: Neo4jStore) -> dict[str, list[str]]:
    """按关系 id 查询证据定位引用（adjustment_generation_spec §15）。"""
    if not path_ids:
        return {}
    rows = store.execute_read(
        "MATCH ()-[r]->() WHERE r.id IN $ids "
        "RETURN r.id AS id, coalesce(r.source_refs, []) AS source_refs",
        {"ids": path_ids},
    )
    return {row["id"]: row["source_refs"] for row in rows}


def query_parameter_chains(
    store: Neo4jStore, parameter_codes: list[str], limit_per_hop: int = 12
) -> dict[str, list[dict]]:
    """查询 Parameter→Mechanism→Quality 推理链（adjustment_generation_spec §8）。

    每条链独立返回、不展平合并（验收规范 P0：保留完整、独立的知识路径）：
    返回 {参数 code: [{"node_codes": [...], "rels": [{id, confidence,
    source_refs}, ...]}]}，链按节点序列去重并确定性排序。
    与 query_reasoning_paths 共用 _MECHANISM_TAILS 固定模板。
    """
    chains: dict[str, dict[tuple, dict]] = {}
    for tail, _aliases in _MECHANISM_TAILS.values():
        rows = store.execute_read(
            f"MATCH p = (pa:Parameter){tail} WHERE pa.code IN $codes "
            f"RETURN pa.code AS code, "
            f"[n IN nodes(p) | coalesce(n.code, n.equipment_id, '')] AS node_codes, "
            f"[r IN relationships(p) | {{id: r.id, confidence: r.confidence, "
            f"source_refs: coalesce(r.source_refs, [])}}] AS rels "
            f"LIMIT $limit",
            {"codes": sorted(parameter_codes), "limit": limit_per_hop},
        )
        for row in rows:
            code = row["code"]
            bucket = chains.setdefault(code, {})
            seq_key = tuple(row["node_codes"])
            if seq_key in bucket:
                continue  # 节点序列重复的链只保留一条
            bucket[seq_key] = {
                "node_codes": list(row["node_codes"]),
                "rels": [dict(rel) for rel in row["rels"]],
            }
    # 按节点序列确定性排序，保证两次运行链顺序一致
    return {
        code: [bucket[key] for key in sorted(bucket)]
        for code, bucket in sorted(chains.items())
    }


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
            for code, limits in query_equipment_limits(store, param_codes).items():
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
