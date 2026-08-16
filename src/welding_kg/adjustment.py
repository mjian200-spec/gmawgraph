"""焊接参数修正量生成（adjustment_generation_spec）。

最小闭环：需求 + 多个相似案例 + 图谱知识 + 设备步长
→ 多个独立修正方案 → 置信度排序 → 推荐参数。

职责边界（规范 §3）：
- 图谱决定可调参数、调整方向和设备步长；
- 案例数据估计修正量的相对大小；
- Python 完成统计、量化和评分；
- LLM 只选择完整知识路径和生成简短说明，不得凭空报数。

本阶段只处理焊接电流（welding_current）、电压（arc_voltage）和
焊接速度（welding_speed）三个参数（SUPPORTED_PARAMETER_CODES 白名单）；
图谱中出现其他带步长参数时安全忽略（验收规范 P0）。
"""

from __future__ import annotations

import asyncio

from .case_comparator import compare_case
from .case_retriever import _FIELD_LABELS, find_similar_cases
from .models import (
    AdjustmentKnowledgePath,
    AdjustmentProposal,
    CaseMatch,
    CaseRecord,
    ConfidenceBreakdown,
    DeltaEstimate,
    EquipmentStep,
    ParameterAdjustment,
    SUPPORTED_PARAMETER_CODES,
    WeldingRequirement,
)
from .neo4j_store import Neo4jStore
# 复用既有图谱查询与 LLM 层（避免两份固定 Cypher 模板与客户端漂移）：
# - 机理链模板 _MECHANISM_TAILS 与 path_retriever 的路径查询共用；
# - LLM 客户端与 JSON 解析复用 path_planner 的实现。
from .path_planner import _llm_client, _parse_selection as _parse_json_object
from .path_retriever import (
    get_adjustment_paths,
    get_equipment_limits,
    get_path_sources,
    query_equipment_limits,
    query_parameter_chains,
)
from .settings import Settings, load_adjustment_config

# 参数 code → 案例/推荐值字段（规范 §5 调整对象与本阶段范围一致）
_PARAM_VALUE_FIELD = {
    "welding_current": "welding_current_a",
    "arc_voltage": "welding_voltage_v",
    "welding_speed": "welding_speed_mm_s",
}

# 参数 code → 方案推荐值字段（AdjustmentProposal）
_PARAM_RECOMMEND_FIELD = {
    "welding_current": "recommended_current_a",
    "arc_voltage": "recommended_voltage_v",
    "welding_speed": "recommended_speed_mm_s",
}

# 参数 code → 人工可读名称（仅用于依据摘要与告警）
_PARAM_NAME = {
    "welding_current": "焊接电流",
    "arc_voltage": "焊接电压",
    "welding_speed": "焊接速度",
}

# 修正依据摘要 JSON 契约（供 LLM 提示词与解析共用）
_BASIS_SCHEMA = (
    '{"selected_path_ids": ["path_01", "path_02"], '
    '"basis": "一句话概括修正依据（只使用给定事实）"}'
)


# ---------------------------------------------------------------------------
# 图谱查询（规范 §15）
#
# get_equipment_limits / get_adjustment_paths / get_path_sources /
# query_parameter_chains 统一放在 path_retriever 查询层并在此重导出，
# 修正量模块只做统计、量化与评分（规范 §3 职责边界）。
# ---------------------------------------------------------------------------


def get_adjustment_steps(
    equipment_id: str, store: Neo4jStore
) -> dict[str, EquipmentStep]:
    """读取设备步长（规范 §4/§6）。

    取值优先级：adjustment_step（说明书正式步长）→ default_step（图谱保存的
    项目默认值）→ 均不存在则不返回该参数，由调用方生成告警。
    只返回本阶段受支持参数（验收规范 P0）：图谱中其他带步长参数被忽略，
    不会进入下游统计与量化。
    """
    cfg = load_adjustment_config()
    steps: dict[str, EquipmentStep] = {}
    for row in get_equipment_limits(equipment_id, None, store):
        props = row["properties"]
        code = row["parameter_code"]
        if code not in SUPPORTED_PARAMETER_CODES:
            continue  # 非本阶段参数，安全忽略
        if props.get("adjustment_step") is not None:
            steps[code] = EquipmentStep(
                parameter_code=code,
                step=float(props["adjustment_step"]),
                unit=str(props.get("unit") or ""),
                source_type="manual",
                confidence=float(cfg["step_confidence"]["manual"]),
                source_refs=list(props.get("source_refs") or []),
            )
        elif props.get("default_step") is not None:
            steps[code] = EquipmentStep(
                parameter_code=code,
                step=float(props["default_step"]),
                unit=str(props.get("unit") or ""),
                source_type="project_default",
                confidence=float(cfg["step_confidence"]["project_default"]),
                source_refs=list(props.get("source_refs") or []),
            )
    return steps


def _get_limits_for_params(
    store: Neo4jStore,
    parameter_codes: list[str],
    equipment_id: str | None,
) -> dict[str, dict]:
    """按参数 code 聚合设备限制属性（范围、控制模式等，规范 §10）。

    equipment_id 给定时只查该设备；否则查全部设备。
    """
    grouped = query_equipment_limits(store, parameter_codes, equipment_id)
    return {
        code: entries[0]["properties"]
        for code, entries in grouped.items()
        if entries
    }


# ---------------------------------------------------------------------------
# 差异 → Condition（规范 §8）
# ---------------------------------------------------------------------------


def _derive_condition_codes(
    target: WeldingRequirement | CaseRecord, base: CaseRecord
) -> tuple[list[str], list[str]]:
    """把目标与基准案例的差异转换为图谱 Condition code（规范 §8）。

    直接复用 case_comparator.compare_case 的差异代码契约（单一来源）：
    数值字段生成 {field}_increase / {field}_decrease；位置差异为
    position_to_vertical / position_to_overhead（比较器已扩展契约）；
    其余类别差异（*_changed）在图谱中无对应规则，返回缺口提示列表。
    返回 (condition codes, 缺口提示)。
    """
    codes: list[str] = []
    gaps: list[str] = []
    for diff in compare_case(target, base):
        if diff.change == "same":
            continue
        if diff.code.endswith("_changed"):
            label = _FIELD_LABELS.get(diff.field, diff.field)
            gap = f"{label}差异（{diff.before}→{diff.after}）在图谱中无调整规则"
            if diff.note:
                gap += f"（{diff.note}）"
            gaps.append(gap)
            continue
        codes.append(diff.code)
    return codes, gaps


# ---------------------------------------------------------------------------
# 基准案例选择（规范 §7）
# ---------------------------------------------------------------------------


def _condition_key(case: CaseRecord) -> tuple:
    """案例全工况签名（材料、厚度、接头、位置、气体、焊丝直径）。"""
    return (
        case.material,
        case.thickness_mm,
        case.joint_type,
        case.position,
        case.wire_diameter_mm,
        case.shielding_gas,
    )


def _difference_signature(
    target: WeldingRequirement, case: CaseRecord
) -> frozenset:
    """案例相对需求的差异类型签名（规范 §7 差异类型去重）。"""
    codes, _ = _derive_condition_codes(target, case)
    return frozenset(codes)


def select_diverse_base_cases(
    matches: list[CaseMatch],
    count: int = 3,
    requirement: WeldingRequirement | None = None,
) -> list[CaseMatch]:
    """从已排序的相似案例中选出最多 count 个基准案例（规范 §6/§7）。

    - 材料、厚度、接头、位置、气体和焊丝直径完全相同只留最高分；
    - 优先保留与需求差异类型不同的案例；
    - 数量不足时按相似度补齐，仍可少于 count（由调用方降分）。
    """
    selected: list[CaseMatch] = []
    seen_keys: set[tuple] = set()
    seen_sigs: list[frozenset] = []
    deferred: list[CaseMatch] = []

    def sig_of(match: CaseMatch) -> frozenset:
        if requirement is not None:
            return _difference_signature(requirement, match.case)
        # 无需求时退化为工况签名多样性
        return frozenset((match.case.material, match.case.joint_type,
                          match.case.position, match.case.shielding_gas))

    for match in matches:
        if len(selected) >= count:
            break
        key = _condition_key(match.case)
        if key in seen_keys:
            continue  # 全工况重复，只留最高分（matches 已按分数降序）
        seen_keys.add(key)
        sig = sig_of(match)
        if not seen_sigs or sig not in seen_sigs:
            selected.append(match)
            seen_sigs.append(sig)
        else:
            deferred.append(match)

    for match in deferred:  # 差异类型重复时按相似度补齐
        if len(selected) >= count:
            break
        selected.append(match)
    return selected[:count]


# ---------------------------------------------------------------------------
# 方向聚合与冲突检测（规范 §8）
# ---------------------------------------------------------------------------


def _aggregate_directions(
    path_rows: list[dict], warnings: list[str]
) -> tuple[dict[str, tuple[str, list[dict]]], set[str]]:
    """按参数聚合调整方向；方向冲突时停止该参数并写告警（规范 §8）。

    返回 (方向表, 被停止的参数 code 集合)：
    方向表 {参数 code: (方向, 规则行列表)}；冲突或方向为 same 的参数
    进入停止集合，不在方向表中。
    """
    by_param: dict[str, list[dict]] = {}
    for row in path_rows:
        by_param.setdefault(row["parameter_code"], []).append(row)

    directions: dict[str, tuple[str, list[dict]]] = {}
    stopped: set[str] = set()
    for code in sorted(by_param):
        rows = by_param[code]
        changes = {row.get("target_change") for row in rows}
        if len(changes) > 1:
            stopped.add(code)
            warnings.append(
                f"参数 {_PARAM_NAME.get(code, code)} 存在方向冲突（{sorted(changes)}），"
                "按规范 §8 停止修正该参数"
            )
            continue
        change = changes.pop()
        if change not in ("increase", "decrease"):
            stopped.add(code)
            warnings.append(f"参数 {_PARAM_NAME.get(code, code)} 的方向为 {change!r}，跳过")
            continue
        directions[code] = (change, rows)
    return directions, stopped


# ---------------------------------------------------------------------------
# 案例幅度估计（规范 §9）
# ---------------------------------------------------------------------------


def _weighted_median(values: list[float], weights: list[float]) -> float:
    """加权中位数：按值排序后累计权重，首个跨过半程的值。"""
    if len(values) == 1:
        return values[0]
    pairs = sorted(zip(values, weights), key=lambda p: p[0])
    total = sum(weights)
    if total <= 0:
        raise ValueError("权重和必须为正")
    half = total / 2
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


def estimate_case_delta(
    base_case: CaseRecord,
    support_cases: list[CaseMatch],
    parameter_code: str,
    direction: str,
) -> DeltaEstimate:
    """估计案例修正量（规范 §6/§9）。

    1. observed_delta = support.parameter - base.parameter；
    2. 删除符号与图谱方向冲突的差值；
    3. 支持案例优先材料一致（无同材料时才用其他材料）；
    4. 相似度加权中位数聚合；
    5. 无有效差值时 raw_delta=None，调用方按正负一个设备步长退化。
    非本阶段参数（图谱异常数据）返回退化估计，不会触发 KeyError。
    """
    field = _PARAM_VALUE_FIELD.get(parameter_code)
    if field is None:
        return DeltaEstimate(
            parameter_code=parameter_code, direction=direction,
            raw_delta=None, fallback_used=True,
        )
    base_value = getattr(base_case, field)
    if base_value is None:
        return DeltaEstimate(
            parameter_code=parameter_code, direction=direction,
            raw_delta=None, fallback_used=True,
        )

    comparable: list[tuple[CaseMatch, float]] = []
    n_filtered, n_missing = 0, 0
    for match in support_cases:
        value = getattr(match.case, field)
        if value is None:
            n_missing += 1
            continue
        delta = float(value) - float(base_value)
        sign_ok = (direction == "increase" and delta > 0) or (
            direction == "decrease" and delta < 0
        )
        if sign_ok:
            comparable.append((match, delta))
        else:
            n_filtered += 1

    # 材料一致优先（规范 §9）
    if comparable:
        same_material = [
            (m, d) for m, d in comparable if m.case.material == base_case.material
        ]
        pool = same_material if same_material else comparable
    else:
        pool = []

    if not pool:
        return DeltaEstimate(
            parameter_code=parameter_code,
            direction=direction,
            raw_delta=None,
            n_valid=0,
            n_filtered=n_filtered,
            n_missing=n_missing,
            fallback_used=True,
        )

    deltas = [d for _, d in pool]
    weights = [float(m.total_score) for m, _ in pool]
    raw_delta = _weighted_median(deltas, weights)

    # 离散度：MAD / |中位数|（对异常案例稳健，规范 §13 case_support）
    dispersion: float | None
    if len(deltas) < 2:
        dispersion = None
    else:
        plain_median = sorted(deltas)[len(deltas) // 2]
        mad = sorted(abs(d - plain_median) for d in deltas)[len(deltas) // 2]
        dispersion = min(1.0, mad / max(abs(plain_median), 1e-9))

    return DeltaEstimate(
        parameter_code=parameter_code,
        direction=direction,
        raw_delta=float(raw_delta),
        n_valid=len(pool),
        n_filtered=n_filtered,
        n_missing=n_missing,
        valid_deltas=deltas,
        dispersion=dispersion,
        support_case_ids=[m.case.case_id for m, _ in pool],
        fallback_used=False,
    )


# ---------------------------------------------------------------------------
# 步长量化与约束检查（规范 §10）
# ---------------------------------------------------------------------------


def _step_decimals(step: float) -> int:
    """步长的小数位数（浮点数按步长小数位舍入，规范 §10）。"""
    text = f"{step:.10f}".rstrip("0").rstrip(".")
    if "." not in text:
        return 0
    return len(text.split(".")[1])


def quantize_delta(raw_delta: float, step: float) -> float:
    """按设备步长量化修正量（规范 §6/§10）。

    step_count = max(1, round(|raw| / step))；量化后保持图谱方向；
    结果按步长小数位舍入。
    """
    if step <= 0:
        raise ValueError(f"步长必须为正：{step}")
    if raw_delta == 0:
        return 0.0
    sign = 1 if raw_delta > 0 else -1
    step_count = max(1, round(abs(raw_delta) / step))
    quantized = sign * step_count * step
    return round(quantized, _step_decimals(step))


def _check_range(
    recommended: float, limit_props: dict
) -> tuple[bool, bool, list[str]]:
    """量化后检查设备范围（规范 §10、验收规范 P1）。

    返回 (是否通过, 是否完成数值核验, 问题列表)：
    - 图谱给出数值上下限：核验并判越界无效，不做静默截断；
    - 只有文字 range_rule 或无范围信息：不编造上下限，通过但
      verified=False，由调用方告警并降低 equipment 置信度。
    """
    low = limit_props.get("min_value")
    high = limit_props.get("max_value")
    if low is None and high is None:
        return True, False, []
    problems: list[str] = []
    if low is not None and recommended < float(low):
        problems.append(f"推荐值 {recommended} 低于设备下限 {low}")
    if high is not None and recommended > float(high):
        problems.append(f"推荐值 {recommended} 超过设备上限 {high}")
    return not problems, True, problems


def _voltage_mode_blocked(
    limit_props: dict, control_mode: str | None
) -> tuple[bool, str]:
    """一元模式下电压是随电流匹配的增益值，无法可靠换算时不按独立伏特值
    修正（规范 §10）。图谱未给出模式规则时不阻断。"""
    unified_rule = limit_props.get("unified_mode_rule")
    if not unified_rule:
        return False, ""
    if control_mode == "unified":
        return True, f"一元模式下电压由电流匹配（{unified_rule}），无法可靠换算，不按独立伏特值修正"
    if control_mode is None:
        return True, "控制模式未知且图谱含一元模式电压匹配规则，无法可靠换算，不按独立伏特值修正"
    return False, ""


# ---------------------------------------------------------------------------
# 相对大小标签（规范 §11）
# ---------------------------------------------------------------------------


def _quantile(sorted_values: list[float], q: float) -> float:
    """线性插值分位点（输入已升序）。"""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def _magnitude_label(
    abs_value: float, step: float, valid_deltas: list[float], cfg: dict
) -> tuple[str, bool]:
    """相对大小标签（规范 §11），返回 (标签, 是否使用步数规则)。

    至少 3 个有效差值按绝对值的 33%/67% 分位点划分；样本不足用步数规则
    （1 步 small，2—3 步 medium，4 步以上 large），并返回步数规则标记
    供调用方降低案例支持得分。
    """
    mag_cfg = cfg["magnitude"]
    abs_deltas = sorted(abs(d) for d in valid_deltas)
    if len(abs_deltas) >= int(mag_cfg["min_cases_for_quantiles"]):
        q_medium = _quantile(abs_deltas, float(mag_cfg["quantile_medium"]))
        q_large = _quantile(abs_deltas, float(mag_cfg["quantile_large"]))
        if abs_value <= q_medium:
            return "small", False
        if abs_value <= q_large:
            return "medium", False
        return "large", False
    step_count = max(1, round(abs_value / step))
    if step_count <= 1:
        return "small", True
    if step_count <= 3:
        return "medium", True
    return "large", True


# ---------------------------------------------------------------------------
# 完整知识路径（验收规范 P0：保留完整、独立的知识路径）
# ---------------------------------------------------------------------------


def _build_knowledge_paths(
    pending: dict[str, tuple[list[dict], list[dict]]],
) -> list[AdjustmentKnowledgePath]:
    """组装完整知识路径：Condition-SUGGESTS_ADJUSTMENT→Parameter
    →Mechanism→[Mechanism]→Quality（规范 §8、验收规范 P0）。

    pending: {参数 code: (方向规则行列表, 推理链列表)}；每条路径独立返回，
    不与其他路径展平合并。path_id 按 (参数, 节点序列, 关系序列) 确定性
    排序编号，两次运行结果一致。无推理链时退化为两节点方向路径。
    """
    raw: list[tuple] = []
    for parameter_code in sorted(pending):
        suggest_rows, chains = pending[parameter_code]
        chain_list = chains or [{"node_codes": [], "rels": []}]
        for row in suggest_rows:
            for chain in chain_list:
                # 查询层返回的链首节点即参数本身（nodes(p) 从 Parameter 起），
                # 组装时去掉重复的参数节点：完整路径 = 条件 → 参数 → 机理 → 质量
                chain_nodes = list(chain["node_codes"])
                if chain_nodes and chain_nodes[0] == parameter_code:
                    chain_nodes = chain_nodes[1:]
                nodes = [row["condition_code"], parameter_code] + chain_nodes
                rels = [
                    {
                        "id": row["id"],
                        "confidence": row.get("confidence"),
                        "source_refs": list(row.get("source_refs") or []),
                    }
                ] + [dict(rel) for rel in chain["rels"]]
                confs = [
                    float(r["confidence"])
                    for r in rels
                    if r.get("confidence") is not None
                ]
                confidence = sum(confs) / len(confs) if confs else 0.0
                refs: list[str] = []
                for rel in rels:
                    for ref in rel.get("source_refs") or []:
                        if ref not in refs:
                            refs.append(ref)
                raw.append(
                    (
                        parameter_code,
                        tuple(nodes),
                        tuple(r["id"] for r in rels),
                        confidence,
                        refs,
                    )
                )
    raw.sort(key=lambda item: (item[0], item[1], item[2]))
    return [
        AdjustmentKnowledgePath(
            path_id=f"path_{index:02d}",
            parameter_code=parameter_code,
            node_codes=list(nodes),
            relationship_ids=list(rel_ids),
            confidence=round(confidence, 4),
            source_refs=refs,
        )
        for index, (parameter_code, nodes, rel_ids, confidence, refs) in enumerate(raw, start=1)
    ]


def _select_paths_deterministic(
    paths: list[AdjustmentKnowledgePath],
) -> set[str]:
    """Python 确定性路径选择（验收规范 P0）：每个参数保留置信度最高的
    完整路径，并列最高全保留。不使用 LLM 时调用。"""
    best: dict[str, float] = {}
    for path in paths:
        current = best.get(path.parameter_code)
        if current is None or path.confidence > current:
            best[path.parameter_code] = path.confidence
    return {
        path.path_id
        for path in paths
        if path.confidence >= best.get(path.parameter_code, 0.0) - 1e-9
    }


# ---------------------------------------------------------------------------
# 置信度（规范 §13）
# ---------------------------------------------------------------------------


def compute_confidence(breakdown: ConfidenceBreakdown, weights: dict) -> float:
    """confidence = Σ(weight × score)，分项均为 0—1（规范 §13）。"""
    total = 0.0
    for key in ("similarity", "knowledge", "case_support", "consensus", "equipment"):
        total += float(weights.get(key, 0.0)) * float(getattr(breakdown, key))
    return round(min(1.0, total), 4)


def _knowledge_score_from_paths(
    paths: list[AdjustmentKnowledgePath],
) -> float:
    """采用路径的置信度均值 × 来源完整度（规范 §13 knowledge）。

    只统计被选中的完整知识路径（验收规范 P0）。"""
    if not paths:
        return 0.0
    mean_conf = sum(p.confidence for p in paths) / len(paths)
    complete = sum(1 for p in paths if p.source_refs) / len(paths)
    return round(mean_conf * complete, 4)


def _case_support_score(adjustments: list[ParameterAdjustment]) -> float:
    """案例支持得分均值（有效支持案例数量和差值离散程度）。

    各参数的 n_valid/dispersion 已在生成阶段折算进 adjustments（见
    _build_adjustment 的 support 字段），这里只做聚合；空调整为 0。
    """
    if not adjustments:
        return 0.0
    return round(
        sum(a.support for a in adjustments) / len(adjustments), 4
    )


def _compute_consensus(proposals: list[AdjustmentProposal]) -> None:
    """方案间一致性：与其他候选推荐值的接近程度（规范 §12/§13）。

    汇总后按确定性顺序计算，写回各方案的 confidence_breakdown.consensus。
    推荐值接近程度用相对距离 1 - |a-b|/max(|a|,|b|)；候选少于 3 个时
    按比例降低（规范 §7）。
    """
    n = len(proposals)
    for i, proposal in enumerate(proposals):
        total, count = 0.0, 0
        for adj in proposal.adjustments:
            field = _PARAM_RECOMMEND_FIELD.get(adj.parameter_code)
            mine = getattr(proposal, field) if field else None
            if mine is None:
                continue
            for j, other in enumerate(proposals):
                if j == i:
                    continue
                theirs = getattr(other, field) if field else None
                if theirs is None:
                    continue
                closeness = max(0.0, 1.0 - abs(mine - theirs) / max(abs(mine), abs(theirs), 1e-9))
                total += closeness
                count += 1
        closeness = total / count if count else 0.0
        factor = min(1.0, (n - 1) / 2.0)  # 3 个候选为 1，少于 3 个按比例降低（规范 §7）
        proposal.confidence_breakdown.consensus = round(closeness * factor, 4)


# ---------------------------------------------------------------------------
# 可选 LLM：完整路径选择 + 依据摘要（规范 §3/§14、验收规范 P0）
# ---------------------------------------------------------------------------


def _llm_select_and_summarize(
    requirement: WeldingRequirement,
    base_case: CaseRecord,
    condition_codes: list[str],
    facts: dict[str, dict],
    candidate_paths: list[AdjustmentKnowledgePath],
) -> tuple[set[str], str, list[str]]:
    """LLM 只从给定完整路径中选子集并写一句话依据（规范 §3/§14）。

    候选为完整知识路径（path_id 由程序按节点/关系序列生成），非法路径 ID
    拒绝并告警；失败、非法输出或空选择时退化为 Python 确定性选择与
    程序化摘要。返回 (选中的路径 id 集合, basis, 告警列表)。
    """
    settings = Settings.from_env()
    deterministic_ids = _select_paths_deterministic(candidate_paths)
    fallback_basis = _programmatic_basis(
        base_case, condition_codes, facts, candidate_paths, deterministic_ids
    )
    if not candidate_paths:
        return set(), fallback_basis, []

    fact_lines = "\n".join(
        f"- {_PARAM_NAME.get(code, code)}：方向 {info['direction']}，"
        f"量化修正 {info['quantized']}，步长 {info['step'].step}，"
        f"支持案例 {info['n_support']} 个"
        for code, info in sorted(facts.items())
    )
    path_lines = "\n".join(
        f"- [{p.path_id}] {_PARAM_NAME.get(p.parameter_code, p.parameter_code)}："
        f"{' → '.join(p.node_codes)}（置信度 {p.confidence}，"
        f"来源 {p.source_refs or ['无']}）"
        for p in candidate_paths
    )
    prompt = (
        "你是焊接工艺参数修正的说明撰写助手。以下修正方案全部由程序基于历史案例与"
        "知识图谱计算得出，你只做两件事：\n"
        "1. 从给定完整知识路径编号中选择你认同的路径（只允许给定编号，也可为空）；\n"
        "2. 用一句话概括修正依据。\n"
        f"基准案例：{base_case.case_id}\n"
        f"需求与基准差异：{', '.join(condition_codes) or '无'}\n"
        f"程序计算的修正事实：\n{fact_lines}\n"
        f"候选完整知识路径（只能从中选择，不得编造）：\n{path_lines}\n\n"
        f"要求：只输出一个 JSON 对象：{_BASIS_SCHEMA}；不得编造参数值、置信度或"
        "来源；不得输出思维链或 JSON 之外的任何内容。"
    )
    try:
        client = _llm_client(settings.llm_base_url, settings.llm_api_key)
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=512,
            response_format={"type": "json_object"},
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 LLM 不可用时不阻断流程
        return deterministic_ids, fallback_basis, [f"LLM 调用失败，已使用确定性路径选择与程序化摘要：{exc}"]

    parsed = _parse_json_object(content)
    if parsed is None:
        return deterministic_ids, fallback_basis, ["LLM 返回无法解析，已使用确定性路径选择与程序化摘要"]

    valid_ids = {p.path_id for p in candidate_paths}
    selected = {pid for pid in parsed.get("selected_path_ids", []) if pid in valid_ids}
    rejected = [pid for pid in parsed.get("selected_path_ids", []) if pid not in valid_ids]
    warnings: list[str] = []
    if rejected:
        warnings.append(f"LLM 选择了不在候选中的路径编号，已拒绝：{rejected}")
    if not selected:
        warnings.append("LLM 未选择任何路径，已使用确定性路径选择")
        selected = set(deterministic_ids)
    basis = str(parsed.get("basis") or "").strip()[:200]
    if not basis:
        warnings.append("LLM 未给出依据摘要，已使用程序化摘要")
        basis = fallback_basis
    return selected, basis, warnings


def _programmatic_basis(
    base_case: CaseRecord,
    condition_codes: list[str],
    facts: dict[str, dict],
    candidate_paths: list[AdjustmentKnowledgePath],
    kept_ids: set[str],
) -> str:
    """程序化依据摘要（LLM 不可用或失败时的确定性退化，规范 §14）。"""
    diff_text = "、".join(condition_codes) if condition_codes else "无"
    parts = [f"需求较基准案例 {base_case.case_id} 的差异：{diff_text}"]
    for code in sorted(facts):
        info = facts[code]
        name = _PARAM_NAME.get(code, code)
        dir_text = {"increase": "增加", "decrease": "降低"}.get(info["direction"], info["direction"])
        support_text = (
            f"支持案例 {info['n_support']} 个" if info["n_support"]
            else "单步退化（无有效支持差值）"
        )
        kept = [p for p in candidate_paths if p.parameter_code == code and p.path_id in kept_ids]
        path_text = "、".join(p.path_id for p in kept) if kept else "无"
        parts.append(
            f"{name} {dir_text} {abs(info['quantized'])}（步长 {info['step'].step}，"
            f"{support_text}，知识路径 {path_text}）"
        )
    return "；".join(parts)


# ---------------------------------------------------------------------------
# 单方案生成（规范 §12）
# ---------------------------------------------------------------------------


def _build_adjustment(
    parameter_code: str,
    direction: str,
    estimate: DeltaEstimate,
    step: EquipmentStep,
    magnitude: str,
    step_rule_used: bool,
    cfg: dict,
    kept_paths: list[AdjustmentKnowledgePath],
    fallback_warnings: list[str],
) -> ParameterAdjustment:
    """组装单参数修正量（规范 §9—§11、验收规范 P0）。

    path_ids 与 source_refs 只来自被选中的完整知识路径（+ 设备步长
    说明书定位），不再从展平的关系并集拼装。
    """
    raw = estimate.raw_delta
    if raw is None:
        raw = step.step if direction == "increase" else -step.step
        fallback_warnings.append(
            f"参数 {_PARAM_NAME.get(parameter_code, parameter_code)} 无有效支持差值，"
            "退化为正负一个设备步长（规范 §9.3）"
        )
    quantized = quantize_delta(raw, step.step)

    # 案例支持折算：有效数量、离散度、退化与步数规则的降分（规范 §13 case_support）
    if estimate.fallback_used:
        support = float(cfg["case_delta"]["fallback_support_factor"])
    else:
        n_factor = min(1.0, estimate.n_valid / float(cfg["case_delta"]["min_valid_for_full_support"]))
        if estimate.dispersion is None:
            penalty = float(cfg["case_delta"]["unknown_dispersion_penalty"])
        else:
            penalty = estimate.dispersion
        support = n_factor * (1 - penalty)
    if step_rule_used:
        support *= float(cfg["case_delta"]["step_rule_penalty"])

    refs: list[str] = []
    path_ids: list[str] = []
    for path in kept_paths:
        for rel_id in path.relationship_ids:
            if rel_id not in path_ids:
                path_ids.append(rel_id)
        for ref in path.source_refs:
            if ref not in refs:
                refs.append(ref)
    for ref in step.source_refs:
        if ref not in refs:
            refs.append(ref)

    return ParameterAdjustment(
        parameter_code=parameter_code,
        direction=direction,  # type: ignore[arg-type]
        raw_delta=round(float(raw), _step_decimals(step.step) + 2),
        quantized_delta=quantized,
        step=step.step,
        magnitude=magnitude,  # type: ignore[arg-type]
        support_case_ids=estimate.support_case_ids,
        path_ids=sorted(path_ids),
        source_refs=refs,
        fallback=estimate.fallback_used,
        support=round(support, 4),  # 中间量：置信度折算用，不进入规范 §5 契约输出
    )


def _generate_case_proposal_sync(
    requirement: WeldingRequirement,
    base_case: CaseMatch,
    support_cases: list[CaseMatch],
    steps: dict[str, EquipmentStep],
    store: Neo4jStore,
    use_llm: bool = True,
    proposal_index: int | None = None,
    equipment_id: str | None = None,
) -> AdjustmentProposal:
    """单个基准案例的修正方案（同步实现，规范 §12）。

    流程：差异 → Condition → 图谱方向（白名单参数，冲突停止）→ 设备步长
    与范围/模式检查 → 案例幅度估计 → 步长量化 → 完整知识路径构建
    → 可选 LLM 路径选择与摘要 → 置信度（consensus 由上层汇总后补算）。
    """
    cfg = load_adjustment_config()
    warnings: list[str] = []
    base_record = base_case.case
    proposal_id = f"prop_{proposal_index:02d}" if proposal_index is not None else f"prop_{base_record.case_id}"

    # 1) 差异 → Condition（规范 §8）
    condition_codes, gaps = _derive_condition_codes(requirement, base_record)
    warnings.extend(gaps)

    # 2) 图谱调整方向：固定受支持参数白名单（规范 §1、验收规范 P0），
    #    不依赖 steps.keys() —— 先查方向、后查步长，缺步长参数才能被
    #    发现并告警（规范 §4.3）
    parameter_codes = list(SUPPORTED_PARAMETER_CODES)
    path_rows = get_adjustment_paths(condition_codes, parameter_codes, store) if condition_codes else []
    ruled_conditions = {row["condition_code"] for row in path_rows}
    for code in condition_codes:
        if code not in ruled_conditions:
            warnings.append(f"条件 {code} 在图谱中无调整规则")

    # 3) 方向聚合 + 冲突检测（规范 §8）
    directions, stopped_codes = _aggregate_directions(path_rows, warnings)
    for code in parameter_codes:
        if code in directions and code not in steps:
            # 有图谱方向支持但无设备步长：停止该参数并告警，不阻止其他参数
            warnings.append(
                f"参数 {_PARAM_NAME.get(code, code)} 有图谱方向支持但无设备步长"
                "（adjustment_step / default_step 均不存在），停止修正（规范 §4）"
            )
        elif code not in directions and code not in stopped_codes:
            # 只修正有图谱方向支持的参数（规范 §8）
            warnings.append(
                f"参数 {_PARAM_NAME.get(code, code)} 在图谱中无调整方向支持，未修正"
            )

    # 4) 设备限制与推理链（规范 §8/§10）
    limits = _get_limits_for_params(store, parameter_codes, equipment_id)
    chains = query_parameter_chains(store, parameter_codes)

    # 5) 需求可比性预过滤（规范 §9：工况变化方向必须与需求差异可比较）
    req_codes = set(condition_codes)
    comparable_support: list[CaseMatch] = []
    for match in support_cases:
        codes, _ = _derive_condition_codes(match.case, base_record)
        if req_codes and not (req_codes & set(codes)):
            continue
        comparable_support.append(match)

    # 6) 逐参数估计与量化（数值全部由程序计算，LLM 不参与）
    facts: dict[str, dict] = {}
    recommended: dict[str, float] = {}
    blocked: list[str] = []  # 设备原因停止的参数（步长缺失/模式阻断）
    range_components: dict[str, float] = {}  # 范围核验折算分（验收规范 P1）
    for parameter_code in sorted(directions):
        direction, suggest_rows = directions[parameter_code]
        step = steps.get(parameter_code)
        if step is None:
            blocked.append(parameter_code)  # 已在第 3 步告警
            continue
        limit_props = limits.get(parameter_code, {})
        if parameter_code == "arc_voltage":
            mode_blocked, reason = _voltage_mode_blocked(limit_props, requirement.control_mode)
            if mode_blocked:
                warnings.append(reason)
                blocked.append(parameter_code)
                continue
        base_value = getattr(base_record, _PARAM_VALUE_FIELD[parameter_code])
        if base_value is None:
            warnings.append(f"基准案例缺少 {_PARAM_NAME.get(parameter_code, parameter_code)} 数值，跳过")
            blocked.append(parameter_code)
            continue

        estimate = estimate_case_delta(
            base_record, comparable_support, parameter_code, direction
        )
        magnitude, step_rule_used = _magnitude_label(
            abs(estimate.raw_delta if estimate.raw_delta is not None else step.step),
            step.step,
            estimate.valid_deltas,
            cfg,
        )
        raw = estimate.raw_delta
        if raw is None:
            raw = step.step if direction == "increase" else -step.step
        quantized = quantize_delta(raw, step.step)
        value = round(float(base_value) + quantized, _step_decimals(step.step))

        in_range, range_verified, range_problems = _check_range(value, limit_props)
        if not in_range:
            # 越界方案判为无效，不做静默截断（规范 §10）
            warnings.extend(range_problems)
            return AdjustmentProposal(
                proposal_id=proposal_id,
                base_case_id=base_record.case_id,
                adjustments=[],
                confidence_breakdown=ConfidenceBreakdown(
                    similarity=0.0, knowledge=0.0, case_support=0.0,
                    consensus=0.0, equipment=0.0,
                ),
                warnings=warnings,
                valid=False,
            )
        if range_verified:
            range_components[parameter_code] = 1.0
        else:
            # 无数值上下限：告警并降 equipment 分，不编造上下限（验收规范 P1）
            range_rule = limit_props.get("range_rule")
            verified = "步长、模式"
            unverified = "数值范围"
            warnings.append(
                f"参数 {_PARAM_NAME.get(parameter_code, parameter_code)} 设备范围无法数值核验"
                f"（仅文本规则：{range_rule or '无'}）；已验证约束：{verified}；未验证约束：{unverified}"
            )
            range_components[parameter_code] = float(cfg["equipment"]["unknown_range_credit"])

        recommended[parameter_code] = value
        facts[parameter_code] = {
            "direction": direction,
            "suggest_rows": suggest_rows,
            "step": step,
            "quantized": quantized,
            "raw": raw,
            "estimate": estimate,
            "magnitude": magnitude,
            "step_rule_used": step_rule_used,
            "n_support": len(estimate.support_case_ids),
        }

    if not facts:
        return AdjustmentProposal(
            proposal_id=proposal_id,
            base_case_id=base_record.case_id,
            adjustments=[],
            confidence_breakdown=ConfidenceBreakdown(
                similarity=0.0, knowledge=0.0, case_support=0.0,
                consensus=0.0, equipment=0.0,
            ),
            warnings=warnings,
            valid=False,
        )

    # 7) 完整知识路径（验收规范 P0）：每条独立，路径 ID 由序列确定性生成
    candidate_paths = _build_knowledge_paths({
        code: (facts[code]["suggest_rows"], chains.get(code, []))
        for code in facts
    })

    # 8) 路径选择 + 依据摘要（可选 LLM；LLM 只选完整路径、不得动数值）
    if use_llm:
        kept_ids, basis, llm_warnings = _llm_select_and_summarize(
            requirement, base_record, condition_codes, facts, candidate_paths
        )
        warnings.extend(llm_warnings)
    else:
        kept_ids = _select_paths_deterministic(candidate_paths)
        basis = _programmatic_basis(base_record, condition_codes, facts, candidate_paths, kept_ids)
    # 兜底：某参数被全拒时按确定性规则补回，保证溯源完整
    for code in facts:
        if not any(p.parameter_code == code and p.path_id in kept_ids for p in candidate_paths):
            fallback = {
                p.path_id for p in candidate_paths
                if p.parameter_code == code and p.confidence >= max(
                    (q.confidence for q in candidate_paths if q.parameter_code == code),
                    default=0.0,
                ) - 1e-9
            }
            kept_ids |= fallback
            warnings.append(
                f"参数 {_PARAM_NAME.get(code, code)} 未被选中任何路径，"
                "已按确定性规则保留最高置信度路径"
            )

    # 9) 组装修正量（path_ids 只来自被选中的完整路径）
    adjustments: list[ParameterAdjustment] = []
    for parameter_code in sorted(facts):
        info = facts[parameter_code]
        kept_paths = [
            p for p in candidate_paths
            if p.parameter_code == parameter_code and p.path_id in kept_ids
        ]
        fallback_warnings: list[str] = []
        adjustment = _build_adjustment(
            parameter_code=parameter_code,
            direction=info["direction"],
            estimate=info["estimate"],
            step=info["step"],
            magnitude=info["magnitude"],
            step_rule_used=info["step_rule_used"],
            cfg=cfg,
            kept_paths=kept_paths,
            fallback_warnings=fallback_warnings,
        )
        warnings.extend(fallback_warnings)
        adjustments.append(adjustment)

    # 10) 置信度（consensus 由上层补算，规范 §13）
    kept_all = [p for p in candidate_paths if p.path_id in kept_ids]
    # equipment：每参数 (步长 + 模式 + 范围) 三分量均值，被停止参数记 0
    equipment_parts: list[float] = []
    for parameter_code in sorted(facts):
        component = (1.0 + 1.0 + range_components.get(parameter_code, 0.0)) / 3.0
        equipment_parts.append(component)
    equipment_parts.extend(0.0 for _ in blocked)
    equipment_score = sum(equipment_parts) / len(equipment_parts) if equipment_parts else 0.0
    breakdown = ConfidenceBreakdown(
        similarity=round(float(base_case.total_score), 4),
        knowledge=_knowledge_score_from_paths(kept_all),
        case_support=_case_support_score(adjustments),
        consensus=0.0,  # 上层汇总后补算（规范 §12）
        equipment=round(equipment_score, 4),
    )
    confidence = compute_confidence(breakdown, cfg["confidence_weights"])
    return AdjustmentProposal(
        proposal_id=proposal_id,
        base_case_id=base_record.case_id,
        adjustments=adjustments,
        base_current_a=base_record.welding_current_a,
        base_voltage_v=base_record.welding_voltage_v,
        base_speed_mm_s=base_record.welding_speed_mm_s,
        recommended_current_a=recommended.get("welding_current"),
        recommended_voltage_v=recommended.get("arc_voltage"),
        recommended_speed_mm_s=recommended.get("welding_speed"),
        confidence=confidence,
        confidence_breakdown=breakdown,
        basis=basis,
        warnings=warnings,
    )


async def generate_case_proposal(
    requirement: WeldingRequirement,
    base_case: CaseMatch,
    support_cases: list[CaseMatch],
    steps: dict[str, EquipmentStep],
    store: Neo4jStore,
    use_llm: bool = True,
) -> AdjustmentProposal:
    """单个基准案例的修正方案（规范 §6/§12，线程池实现真正并行）。"""
    return await asyncio.to_thread(
        _generate_case_proposal_sync,
        requirement, base_case, support_cases, steps, store, use_llm,
    )


def generate_case_proposal_sync(
    requirement: WeldingRequirement,
    base_case: CaseMatch,
    support_cases: list[CaseMatch],
    steps: dict[str, EquipmentStep],
    store: Neo4jStore,
    use_llm: bool = True,
) -> AdjustmentProposal:
    """同步包装（规范 §6，供 CLI 与测试调用）。"""
    return _generate_case_proposal_sync(
        requirement, base_case, support_cases, steps, store, use_llm,
    )


# ---------------------------------------------------------------------------
# 多方案并行生成与汇总（规范 §2/§12/§13）
# ---------------------------------------------------------------------------


def _validate_counts(top_k: int, proposal_count: int) -> None:
    """强制上限（验收规范 P1）：最多检索 5 个案例、最多生成 3 个方案。"""
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 5:
        raise ValueError(f"top_k 必须在 [1, 5]（规范 §2：最多检索 5 个案例），当前为 {top_k!r}")
    if not isinstance(proposal_count, int) or isinstance(proposal_count, bool) or not 1 <= proposal_count <= 3:
        raise ValueError(f"proposal_count 必须在 [1, 3]（规范 §2：最多生成 3 个方案），当前为 {proposal_count!r}")


def _finalize_proposals(
    generated: list[AdjustmentProposal],
    warnings: list[str],
) -> list[AdjustmentProposal]:
    """汇总：确定性顺序 → 方案间一致性 → 置信度 → 过滤无效 → 排序
    → 仅对最高分方案产生全局低置信度提示（验收规范 P1）。"""
    cfg = load_adjustment_config()
    valid = [p for p in generated if p.valid and p.adjustments]
    for proposal in generated:
        if not (proposal.valid and proposal.adjustments):
            if proposal.warnings:
                warnings.append(
                    f"基准案例 {proposal.base_case_id} 未产生有效修正，已排除："
                    + "；".join(proposal.warnings[:3])
                )
    # 候选少于 3 个时降低案例支持与一致性得分（规范 §7）
    base_factor = min(1.0, len(valid) / 3.0)
    for proposal in valid:
        proposal.confidence_breakdown.case_support = round(
            proposal.confidence_breakdown.case_support * base_factor, 4
        )
    _compute_consensus(valid)
    weights = cfg["confidence_weights"]
    threshold = float(cfg["low_confidence_threshold"])
    # 恢复确定性顺序（规范 §12）：按置信度降序，同分按 proposal_id 升序
    for proposal in valid:
        proposal.confidence = compute_confidence(proposal.confidence_breakdown, weights)
    valid.sort(key=lambda p: (-p.confidence, p.proposal_id))
    # 各候选的低分状态标记（不产生告警）；全局低置信度提示只针对最高分方案
    for proposal in valid:
        proposal.low_confidence = proposal.confidence < threshold
    if valid and valid[0].low_confidence:
        top = valid[0]
        warnings.append(
            f"最高分方案 {top.proposal_id} 置信度 {top.confidence} 低于阈值 {threshold}，"
            "标记为低置信度建议"
        )
    return valid


def _prepare_recommendation_inputs(
    requirement: WeldingRequirement,
    equipment_id: str,
    top_k: int,
    proposal_count: int,
    store: Neo4jStore,
    global_warnings: list[str],
) -> tuple[list[CaseMatch], dict[str, EquipmentStep], list[CaseMatch]] | None:
    """检索相似案例与设备步长，选出基准案例（同步/异步入口共用）。

    无案例或无步长时写入告警并返回 None（调用方直接返回空列表）。
    """
    _validate_counts(top_k, proposal_count)
    matches = find_similar_cases(requirement, store, top_k=top_k)
    if not matches:
        global_warnings.append(f"未找到工艺为 {requirement.process!r} 的相似案例，无法生成修正方案")
        return None
    for warn in matches[0].warnings:
        if warn not in global_warnings:
            global_warnings.append(warn)

    steps = get_adjustment_steps(equipment_id, store)
    if not steps:
        global_warnings.append(
            f"设备 {equipment_id} 无任何 LIMITS 步长定义（adjustment_step / default_step），"
            "无法生成修正方案（规范 §4）"
        )
        return None

    base_cases = select_diverse_base_cases(matches, count=proposal_count, requirement=requirement)
    return matches, steps, base_cases


def _proposal_work_items(
    requirement: WeldingRequirement,
    matches: list[CaseMatch],
    steps: dict[str, EquipmentStep],
    base_cases: list[CaseMatch],
    use_llm: bool,
    store: Neo4jStore,
    equipment_id: str,
) -> list[tuple]:
    """每个基准案例一份并行工作参数（index 决定 proposal_id，顺序确定）。"""
    support_by_base = {
        base.case.case_id: [m for m in matches if m.case.case_id != base.case.case_id]
        for base in base_cases
    }
    return [
        (
            requirement, base, support_by_base[base.case.case_id], steps, store,
            use_llm, index, equipment_id,
        )
        for index, base in enumerate(base_cases, start=1)
    ]


async def generate_adjustment_recommendations(
    requirement: WeldingRequirement,
    equipment_id: str,
    top_k: int = 5,
    proposal_count: int = 3,
    use_llm: bool = True,
    warnings: list[str] | None = None,
    store: Neo4jStore | None = None,
) -> list[AdjustmentProposal]:
    """异步生成并排序修正方案（规范 §2/§6/§12/§13）。

    检索 Top-K 案例（1—5）→ 选择最多 3 个基准案例 → asyncio 并行独立生成
    → 汇总计算一致性 → 置信度排序（最高分方案为首项）。
    warnings 提供时收集全局告警；top_k / proposal_count 越界抛 ValueError；
    store 提供时复用（单元测试注入 FakeStore），否则自建并负责关闭。
    """
    global_warnings = warnings if warnings is not None else []
    owned = store is None
    store = store or Neo4jStore(Settings.from_env())
    try:
        inputs = _prepare_recommendation_inputs(
            requirement, equipment_id, top_k, proposal_count, store, global_warnings
        )
        if inputs is None:
            return []
        matches, steps, base_cases = inputs

        work_items = _proposal_work_items(
            requirement, matches, steps, base_cases, use_llm, store, equipment_id
        )
        tasks = [
            asyncio.to_thread(_generate_case_proposal_sync, *item)
            for item in work_items
        ]
        generated = list(await asyncio.gather(*tasks))
        return _finalize_proposals(generated, global_warnings)
    finally:
        if owned:
            store.close()


def generate_adjustment_recommendations_sync(
    requirement: WeldingRequirement,
    equipment_id: str,
    top_k: int = 5,
    proposal_count: int = 3,
    use_llm: bool = True,
    warnings: list[str] | None = None,
    store: Neo4jStore | None = None,
) -> list[AdjustmentProposal]:
    """同步包装（规范 §6，供 CLI 与测试调用）。

    直接使用线程池并行生成（concurrent.futures.ThreadPoolExecutor），
    不经过 asyncio.run / asyncio.to_thread：事件循环的关闭流程在受限
    环境（无事件循环能力/受限线程调度）下可能无法干净退出，同步调用方
    不应为此承担代价（验收反馈：单元测试在 asyncio.run 关闭线程池阶段
    阻塞超时）。并行语义与异步版一致：每基准案例一任务、结果按提交顺序
    汇总后确定性排序。
    """
    from concurrent.futures import ThreadPoolExecutor

    global_warnings = warnings if warnings is not None else []
    owned = store is None
    store = store or Neo4jStore(Settings.from_env())
    try:
        inputs = _prepare_recommendation_inputs(
            requirement, equipment_id, top_k, proposal_count, store, global_warnings
        )
        if inputs is None:
            return []
        matches, steps, base_cases = inputs

        work_items = _proposal_work_items(
            requirement, matches, steps, base_cases, use_llm, store, equipment_id
        )
        with ThreadPoolExecutor(
            max_workers=min(proposal_count, max(1, len(work_items))),
            thread_name_prefix="gawg-adjust",
        ) as pool:
            generated = list(
                pool.map(lambda item: _generate_case_proposal_sync(*item), work_items)
            )
        return _finalize_proposals(generated, global_warnings)
    finally:
        if owned:
            store.close()
