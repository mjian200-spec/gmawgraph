"""案例检索层（规范 §9 案例查询与差异层、§10 相似案例算法）。

先按 process 过滤，再进行混合相似度排序：
final_score = 0.8 * structured_score + 0.2 * semantic_score；
BGE-M3 不可用时退化为纯结构化检索并返回 warning。
"""

from __future__ import annotations

import math
from functools import lru_cache

from openai import OpenAI

import httpx2  # openai 3.x 的底层 HTTP 客户端库

from .models import CaseMatch, CaseRecord, WeldingRequirement
from .neo4j_store import Neo4jStore
from .settings import Settings, load_retrieval_config

# Case 检索字段名与案例文本中的人工可读字段名
_FIELD_LABELS = {
    "material": "母材材质",
    "thickness_mm": "板厚",
    "joint_type": "接头形式",
    "position": "焊接位置",
    "wire_diameter_mm": "焊丝直径",
    "shielding_gas": "保护气体",
}


@lru_cache(maxsize=8)
def _embed_client(base_url: str, api_key: str) -> OpenAI:
    """按 (base_url, api_key) 缓存 OpenAI 兼容客户端实例。

    trust_env=False：本机服务不走系统代理（环境变量 ALL_PROXY 等）。
    """
    http_client = httpx2.Client(trust_env=False, timeout=120.0)
    return OpenAI(base_url=base_url, api_key=api_key or "EMPTY", http_client=http_client)


def embed_text(text: str, settings: Settings) -> list[float] | None:
    """调用 BGE-M3 嵌入服务计算文本向量（OpenAI 兼容接口）。

    失败返回 None，调用方降级为纯结构化检索并记录 warning。
    """
    if not text.strip():
        return None
    try:
        client = _embed_client(settings.embedding_base_url, settings.embedding_api_key)
        response = client.embeddings.create(model=settings.embedding_model, input=[text])
        return response.data[0].embedding
    except Exception:  # noqa: BLE001 嵌入失败一律降级，不向上抛
        return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度（范围约 [-1, 1]，裁剪到 [0, 1]）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def build_requirement_text(requirement: WeldingRequirement) -> str:
    """把需求结构化字段拼成检索文本（与案例 retrieval_text 对齐）。"""
    parts = [f"工艺：{requirement.process}"]
    for field, label in _FIELD_LABELS.items():
        value = getattr(requirement, field)
        if value is not None:
            parts.append(f"{label}：{value}")
    return "，".join(parts)


def get_case(case_id: str, store: Neo4jStore) -> CaseRecord | None:
    """按 case_id 读取单个案例，不存在时返回 None。"""
    rows = store.execute_read(
        "MATCH (n:Case {case_id: $cid}) RETURN properties(n) AS p", {"cid": case_id}
    )
    if not rows:
        return None
    props = rows[0]["p"]
    case = CaseRecord(**{k: v for k, v in props.items() if k != "props_hash"})
    _enrich_case_refs(store, [case])
    return case


def _enrich_case_refs(store: Neo4jStore, cases: list[CaseRecord]) -> None:
    """从 HAS_* 关系回填案例的概念引用（conditions/parameters/results）。

    数据库中引用以关系形式存在（规范 §4），读取时还原为
    "label_lower:key" 引用字符串，保证 CaseRecord 完整。
    """
    if not cases:
        return
    ids = [c.case_id for c in cases]
    # 一次查询取回三类引用（UNION ALL 合并）
    rows = store.execute_read(
        "MATCH (c:Case)-[:HAS_CONDITION]->(x:Condition) WHERE c.case_id IN $ids "
        "RETURN c.case_id AS cid, 'condition:' + x.code AS ref, 'conditions' AS kind "
        "UNION ALL "
        "MATCH (c:Case)-[:HAS_PARAMETER]->(x:Parameter) WHERE c.case_id IN $ids "
        "RETURN c.case_id AS cid, 'parameter:' + x.code AS ref, 'parameters' AS kind "
        "UNION ALL "
        "MATCH (c:Case)-[:HAS_RESULT]->(x:Quality) WHERE c.case_id IN $ids "
        "RETURN c.case_id AS cid, 'quality:' + x.code AS ref, 'results' AS kind",
        {"ids": ids},
    )
    ref_map: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        entry = ref_map.setdefault(row["cid"], {"conditions": [], "parameters": [], "results": []})
        entry[row["kind"]].append(row["ref"])
    for case in cases:
        entry = ref_map.get(case.case_id)
        if entry:
            case.conditions = sorted(entry["conditions"])
            case.parameters = sorted(entry["parameters"])
            case.results = sorted(entry["results"])


def _structured_score(
    requirement: WeldingRequirement, case: CaseRecord, fields_cfg: dict
) -> tuple[float, dict[str, float]]:
    """计算结构化得分（规范 §10）。

    类别字段相同得 1、不同得 0；数值字段用相对距离公式
    score = max(0, 1 - |a-b| / max(|a|, |b|, 1e-6))；
    缺失字段不计分，其余权重重新归一化。
    """
    total_weight = 0.0
    weighted_sum = 0.0
    field_scores: dict[str, float] = {}
    for field, cfg in fields_cfg.items():
        req_value = getattr(requirement, field)
        case_value = getattr(case, field)
        if req_value is None or case_value is None:
            continue  # 任一侧缺失不计分（权重归一化时自然排除）
        weight = float(cfg.get("weight", 1.0))
        if cfg.get("type") == "numeric":
            a, b = float(req_value), float(case_value)
            score = max(0.0, 1.0 - abs(a - b) / max(abs(a), abs(b), 1e-6))
        else:  # categorical
            score = 1.0 if str(req_value) == str(case_value) else 0.0
        field_scores[field] = score
        total_weight += weight
        weighted_sum += weight * score
    if total_weight == 0:
        return 0.0, field_scores
    return weighted_sum / total_weight, field_scores


def find_similar_cases(
    requirement: WeldingRequirement, store: Neo4jStore, top_k: int = 5
) -> list[CaseMatch]:
    """检索与需求最相似的 Top-K 案例（规范 §9/§10）。

    流程：按 process 硬过滤 → 结构化得分 + 语义得分（BGE-M3 余弦）
    → 混合加权 → 降序取 top_k。LLM 不参与排序。
    """
    settings = Settings.from_env()
    cfg = load_retrieval_config()
    fields_cfg = cfg["structured_fields"]
    warnings: list[str] = []

    # 1) 按 process 过滤，读取全部候选案例
    rows = store.execute_read(
        f"MATCH (n:Case {{process: $p}}) RETURN properties(n) AS p LIMIT 1000",
        {"p": requirement.process},
    )
    if not rows:
        return []  # 无候选：返回空列表，由上层生成 warning

    # 2) 需求语义向量（失败则整体退化）
    requirement_embedding = embed_text(build_requirement_text(requirement), settings)
    if requirement_embedding is None:
        warnings.append("嵌入服务不可用，已退化为纯结构化检索")

    # 3) 逐案例打分
    matches: list[CaseMatch] = []
    cases: list[CaseRecord] = []
    for row in rows:
        props = row["p"]
        cases.append(CaseRecord(**{k: v for k, v in props.items() if k != "props_hash"}))
    _enrich_case_refs(store, cases)  # 回填 HAS_* 概念引用
    for case in cases:
        structured, field_scores = _structured_score(requirement, case, fields_cfg)

        # 语义得分：案例已有 embedding 直接复用，否则按检索文本现算
        case_embedding = case.embedding
        if case_embedding is None and case.retrieval_text and requirement_embedding is not None:
            case_embedding = embed_text(case.retrieval_text, settings)
        if requirement_embedding is not None and case_embedding is not None:
            semantic = _cosine_similarity(requirement_embedding, case_embedding)
        else:
            semantic = 0.0

        total = (
            float(cfg["structured_weight"]) * structured
            + float(cfg["semantic_weight"]) * semantic
        )
        matches.append(
            CaseMatch(
                case=case,
                total_score=round(total, 6),
                structured_score=round(structured, 6),
                semantic_score=round(semantic, 6),
                field_scores={k: round(v, 6) for k, v in field_scores.items()},
                warnings=list(warnings),
            )
        )

    # 4) 按总分降序取 top_k
    matches.sort(key=lambda m: m.total_score, reverse=True)
    return matches[:top_k]
