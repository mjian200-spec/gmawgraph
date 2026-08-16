"""修正量生成单元测试（验收规范 P1：无外部服务也必须全部通过）。

覆盖：纯函数（量化/选择/估计/标签/冲突/范围/模式/置信度）、模型校验、
FakeStore 端到端生成、部分缺步长、非本阶段参数、未知范围降分、
数量上限、LLM 桩的越界路径拒绝与数值不可变性、两次运行确定性。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import (
    FakeStore,
    RaisingLLMClient,
    StubLLMClient,
    build_fake_store,
    load_example_requirement,
    make_case,
    make_match,
)
from welding_kg.adjustment import (
    _aggregate_directions,
    _build_knowledge_paths,
    _check_range,
    _magnitude_label,
    _select_paths_deterministic,
    _voltage_mode_blocked,
    compute_confidence,
    estimate_case_delta,
    generate_adjustment_recommendations_sync,
    quantize_delta,
    select_diverse_base_cases,
)
from welding_kg.models import (
    AdjustmentKnowledgePath,
    AdjustmentProposal,
    CaseMatch,
    CaseRecord,
    ConfidenceBreakdown,
    EquipmentStep,
    ParameterAdjustment,
    WeldingRequirement,
)
from welding_kg.settings import load_adjustment_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EQUIPMENT_ID = "fake_welder"


# ---------------------------------------------------------------------------
# 规范 §16.4：代码中不存在参数步长常量
# ---------------------------------------------------------------------------


def test_code_has_no_hardcoded_step_constants():
    """步长必须查询图谱，禁止在实现代码中硬编码（规范 §4）。"""
    files = [
        PROJECT_ROOT / "src" / "welding_kg" / "adjustment.py",
        PROJECT_ROOT / "scripts" / "recommend_adjustment.py",
    ]
    forbidden = ["5.0", "0.2", "0.5"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for literal in forbidden:
            assert literal not in text, f"{path} 中硬编码了参数步长 {literal}"


# ---------------------------------------------------------------------------
# 规范 §16.2/§16.7：量化保持步长整数倍与方向
# ---------------------------------------------------------------------------


def test_quantize_delta_keeps_step_multiples_and_direction():
    """修正量为设备步长的整数倍，且量化后保持图谱方向（规范 §10）。"""
    assert quantize_delta(12.0, 5.0) == 10.0
    assert quantize_delta(13.0, 5.0) == 15.0
    assert quantize_delta(-13.0, 5.0) == -15.0
    assert quantize_delta(0.45, 0.2) == 0.4
    assert quantize_delta(0.35, 0.2) == 0.4
    assert quantize_delta(1.1, 0.5) == 1.0
    assert quantize_delta(-0.3, 0.5) == -0.5  # 至少 1 个设备步长
    assert quantize_delta(0.0, 5.0) == 0.0

    for raw, step in [(7.3, 5.0), (0.37, 0.2), (0.77, 0.5), (-2.6, 0.5), (11.9, 5.0)]:
        quantized = quantize_delta(raw, step)
        ratio = abs(quantized) / step
        assert abs(ratio - round(ratio)) < 1e-9, f"{raw}/{step} → {quantized}"
        if raw > 0:
            assert quantized > 0
        elif raw < 0:
            assert quantized < 0


# ---------------------------------------------------------------------------
# 规范 §7/§16.8：基准案例选择（去重 + 差异类型多样 + 数量上限）
# ---------------------------------------------------------------------------


def test_select_diverse_base_cases_dedup_and_cap():
    """全工况相同的案例只留最高分；最多 count 个；结果顺序确定。"""
    base = make_case("c_base")
    dup_low = make_case("c_dup_low")
    other = make_case("c_other", joint="lap", position="flat")
    matches = [
        make_match(base, 0.95),
        make_match(dup_low, 0.90),  # 与 base 全工况相同 → 去重
        make_match(other, 0.85),
    ]
    selected = select_diverse_base_cases(matches, count=2)
    assert [m.case.case_id for m in selected] == ["c_base", "c_other"]
    assert len(selected) <= 2


def test_select_diverse_base_cases_prefers_different_difference_types():
    """优先保留与需求差异类型不同的案例；差异类型重复时按相似度补齐。"""
    requirement = WeldingRequirement(process="GMAW", thickness_mm=6.0, position="vertical")
    a = make_match(make_case("c_a", thickness=3.0), 0.9)
    b = make_match(make_case("c_b", thickness=4.0), 0.8)  # 与 a 同为 thickness_increase
    c = make_match(make_case("c_c", thickness=3.0, position="flat"), 0.7)
    selected = select_diverse_base_cases([a, b, c], count=3, requirement=requirement)
    assert selected[0].case.case_id == "c_a"
    assert selected[1].case.case_id == "c_c"  # 差异类型不同者优先于同类型的 b
    assert selected[2].case.case_id == "c_b"  # 补齐


# ---------------------------------------------------------------------------
# 规范 §16.5/§16.6：方向过滤 + 加权中位数稳健性
# ---------------------------------------------------------------------------


def test_estimate_delta_filters_conflicting_sign():
    """与图谱方向冲突的案例差值被过滤（规范 §9.2）。"""
    base = make_case("c_base", current=100.0)
    supports = [
        make_match(make_case("c_up1", current=120.0), 0.9),
        make_match(make_case("c_down", current=85.0), 0.8),  # 方向冲突 → 过滤
        make_match(make_case("c_up2", current=110.0), 0.7),
    ]
    est = estimate_case_delta(base, supports, "welding_current", "increase")
    assert est.n_valid == 2
    assert est.n_filtered == 1
    assert est.support_case_ids == ["c_up1", "c_up2"]
    # 加权中位数：[10(w=.7), 20(w=.9)] → 20
    assert est.raw_delta == 20.0
    assert not est.fallback_used


def test_estimate_delta_outlier_does_not_move_median():
    """异常案例不显著影响加权中位数（规范 §16.6）。"""
    base = make_case("c_base", current=100.0)
    supports = [
        make_match(make_case("c_a", current=105.0), 0.9),
        make_match(make_case("c_b", current=105.0), 0.8),
        make_match(make_case("c_outlier", current=140.0), 0.7),  # 异常大差值
    ]
    est = estimate_case_delta(base, supports, "welding_current", "increase")
    assert est.raw_delta == 5.0  # 中位数不受异常值大小影响
    assert est.dispersion is not None


def test_estimate_delta_falls_back_when_no_valid_delta():
    """无有效差值时退化为单步（raw_delta=None + fallback_used，规范 §9.3）。"""
    base = make_case("c_base", current=100.0)
    supports = [
        make_match(make_case("c_down", current=90.0), 0.9),
    ]
    est = estimate_case_delta(base, supports, "welding_current", "increase")
    assert est.fallback_used
    assert est.raw_delta is None
    assert est.n_valid == 0


def test_estimate_delta_unsupported_parameter_no_keyerror():
    """非本阶段参数（图谱异常数据）安全返回退化估计，不触发 KeyError。"""
    est = estimate_case_delta(
        make_case("c_base"), [make_match(make_case("c_s"), 0.9)],
        "wire_extension", "increase",
    )
    assert est.fallback_used and est.raw_delta is None


# ---------------------------------------------------------------------------
# 规范 §11：相对大小标签
# ---------------------------------------------------------------------------


def test_magnitude_labels_quantile_and_step_rule():
    """≥3 个有效差值按分位点划分；样本不足用步数规则并降案例支持。"""
    cfg = load_adjustment_config()
    # 分位点分支：[10, 20, 30] → 33% 分位 16.6，67% 分位 23.4
    label, step_rule = _magnitude_label(15.0, 5.0, [10.0, 20.0, 30.0], cfg)
    assert label == "small" and not step_rule
    label, step_rule = _magnitude_label(20.0, 5.0, [10.0, 20.0, 30.0], cfg)
    assert label == "medium" and not step_rule
    label, step_rule = _magnitude_label(25.0, 5.0, [10.0, 20.0, 30.0], cfg)
    assert label == "large" and not step_rule
    # 步数规则分支：1 步 small，2—3 步 medium，4 步以上 large
    label, step_rule = _magnitude_label(5.0, 5.0, [10.0], cfg)
    assert label == "small" and step_rule
    label, step_rule = _magnitude_label(15.0, 5.0, [10.0], cfg)
    assert label == "medium" and step_rule
    label, step_rule = _magnitude_label(20.0, 5.0, [10.0], cfg)
    assert label == "large" and step_rule


# ---------------------------------------------------------------------------
# 规范 §16.13 / 验收 P1：方向冲突 / 范围未知与越界 / 模式约束
# ---------------------------------------------------------------------------


def test_direction_conflict_stops_parameter_with_warning():
    """同一参数存在方向冲突时停止该参数并写入告警（规范 §8）。"""
    warnings: list[str] = []
    rows = [
        {"id": "rule:a", "condition_code": "condition:thickness_increase",
         "parameter_code": "welding_current", "target_change": "increase",
         "confidence": 0.8, "source_refs": ["ref:a"]},
        {"id": "rule:b", "condition_code": "condition:position_to_vertical",
         "parameter_code": "welding_current", "target_change": "decrease",
         "confidence": 0.82, "source_refs": ["ref:b"]},
        {"id": "rule:c", "condition_code": "condition:thickness_increase",
         "parameter_code": "welding_speed", "target_change": "decrease",
         "confidence": 0.8, "source_refs": ["ref:c"]},
    ]
    directions, stopped = _aggregate_directions(rows, warnings)
    assert "welding_current" not in directions
    assert "welding_current" in stopped
    assert directions["welding_speed"][0] == "decrease"
    assert any("方向冲突" in w for w in warnings)


def test_range_unknown_not_treated_as_verified():
    """只有文字 range_rule、无数值上下限：不编造上下限，返回未核验。"""
    ok, verified, problems = _check_range(150.0, {"range_rule": "电流曲线匹配范围"})
    assert ok and not verified and not problems


def test_range_violation_marks_invalid_without_truncation():
    """数值上下限存在时核验，越界判为无效，不做静默截断（规范 §10）。"""
    ok, verified, problems = _check_range(160.0, {"min_value": 50, "max_value": 150})
    assert not ok and verified and problems
    ok, verified, problems = _check_range(100.0, {"min_value": 50, "max_value": 150})
    assert ok and verified and not problems


def test_unified_mode_voltage_not_adjusted_as_independent_volts():
    """一元模式（或模式未知且图谱含匹配规则）下电压不得按独立伏特值修正。"""
    props = {"unified_mode_rule": "实际焊接电压由焊接电流匹配，机器人设置值为百分比增益"}
    blocked, _ = _voltage_mode_blocked(props, "unified")
    assert blocked
    blocked, _ = _voltage_mode_blocked(props, None)  # 模式未知 → 保守阻断
    assert blocked
    blocked, _ = _voltage_mode_blocked(props, "separate")  # 分别模式允许独立设置
    assert not blocked
    blocked, _ = _voltage_mode_blocked({}, "unified")  # 图谱无模式规则时不阻断
    assert not blocked


# ---------------------------------------------------------------------------
# 规范 §16.10 / 验收 P1：置信度与配置校验
# ---------------------------------------------------------------------------


def test_confidence_equals_config_weighted_sum():
    """confidence = Σ(weight × score)（规范 §13）。"""
    cfg = load_adjustment_config()
    weights = cfg["confidence_weights"]
    assert sum(float(w) for w in weights.values()) == 1.0
    breakdown = ConfidenceBreakdown(
        similarity=0.9, knowledge=0.85, case_support=0.6, consensus=0.8, equipment=1.0
    )
    expected = (
        0.9 * float(weights["similarity"])
        + 0.85 * float(weights["knowledge"])
        + 0.6 * float(weights["case_support"])
        + 0.8 * float(weights["consensus"])
        + 1.0 * float(weights["equipment"])
    )
    assert compute_confidence(breakdown, weights) == round(expected, 4)


def test_invalid_config_weights_rejected(tmp_path, monkeypatch):
    """配置加载时校验权重非负且总和为 1，非法配置直接报错（验收 P1）。"""
    import welding_kg.settings as settings_mod

    bad = tmp_path / "adjustment.yaml"
    bad.write_text(
        "confidence_weights:\n"
        "  similarity: 0.3\n"
        "  knowledge: 0.2\n"
        "  case_support: 0.0\n"
        "  consensus: 0.0\n"
        "  equipment: 0.0\n"
        "equipment:\n"
        "  unknown_range_credit: 0.5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_mod, "CONFIG_DIR", tmp_path)
    settings_mod.load_adjustment_config.cache_clear()
    with pytest.raises(ValueError, match="权重之和必须为 1"):
        settings_mod.load_adjustment_config()
    settings_mod.load_adjustment_config.cache_clear()

    neg = tmp_path / "adjustment.yaml"
    neg.write_text(
        "confidence_weights:\n"
        "  similarity: 1.1\n"
        "  knowledge: -0.1\n"
        "  case_support: 0.0\n"
        "  consensus: 0.0\n"
        "  equipment: 0.0\n",
        encoding="utf-8",
    )
    settings_mod.load_adjustment_config.cache_clear()
    with pytest.raises(ValueError, match="缺失或为负"):
        settings_mod.load_adjustment_config()
    settings_mod.load_adjustment_config.cache_clear()


# ---------------------------------------------------------------------------
# 验收 P1：数据模型验证
# ---------------------------------------------------------------------------


def test_model_validators_reject_invalid_data():
    """模型层自检：步长为正、置信度分项 [0,1]、受支持参数、方向与符号、
    步长整数倍、推荐值 = 基准值 + 量化修正量。"""
    # EquipmentStep：步长必须为正、参数必须在白名单
    with pytest.raises(ValueError):
        EquipmentStep(parameter_code="welding_current", step=-1.0, unit="A",
                      source_type="manual", confidence=1.0)
    with pytest.raises(ValueError):
        EquipmentStep(parameter_code="wire_extension", step=1.0, unit="mm",
                      source_type="manual", confidence=1.0)

    # ConfidenceBreakdown：分项必须在 [0, 1]
    with pytest.raises(ValueError):
        ConfidenceBreakdown(similarity=1.2, knowledge=0.5, case_support=0.5,
                            consensus=0.5, equipment=0.5)
    with pytest.raises(ValueError):
        ConfidenceBreakdown(similarity=0.5, knowledge=-0.1, case_support=0.5,
                            consensus=0.5, equipment=0.5)

    # CaseMatch：相似度分数必须在 [0, 1]
    with pytest.raises(ValueError):
        CaseMatch(case=make_case("c"), total_score=1.5, structured_score=1.5,
                  semantic_score=0.0)

    # ParameterAdjustment：方向与符号一致、步长整数倍、参数白名单
    with pytest.raises(ValueError):
        ParameterAdjustment(parameter_code="welding_current", direction="increase",
                            raw_delta=10.0, quantized_delta=-10.0, step=5.0,
                            magnitude="medium")
    with pytest.raises(ValueError):
        ParameterAdjustment(parameter_code="welding_current", direction="increase",
                            raw_delta=10.0, quantized_delta=12.0, step=5.0,
                            magnitude="medium")
    with pytest.raises(ValueError):
        ParameterAdjustment(parameter_code="wire_extension", direction="increase",
                            raw_delta=1.0, quantized_delta=1.0, step=1.0,
                            magnitude="small")

    # AdjustmentProposal：推荐值必须等于基准值 + 量化修正量
    good_adj = ParameterAdjustment(parameter_code="welding_current", direction="increase",
                                   raw_delta=10.0, quantized_delta=10.0, step=5.0,
                                   magnitude="medium")
    with pytest.raises(ValueError):
        AdjustmentProposal(
            proposal_id="prop_01", base_case_id="c", adjustments=[good_adj],
            base_current_a=100.0, recommended_current_a=999.0,
            confidence_breakdown=ConfidenceBreakdown(
                similarity=0.9, knowledge=0.8, case_support=0.5,
                consensus=0.5, equipment=1.0,
            ),
        )

    # AdjustmentKnowledgePath：关系数必须等于节点数减 1
    with pytest.raises(ValueError):
        AdjustmentKnowledgePath(
            path_id="path_01", parameter_code="welding_current",
            node_codes=["a", "b", "c"], relationship_ids=["r1"], confidence=0.9,
        )


# ---------------------------------------------------------------------------
# 验收 P0：完整、独立的知识路径
# ---------------------------------------------------------------------------


def test_knowledge_paths_are_complete_and_independent():
    """每条路径独立返回：Condition→Parameter→Mechanism→Quality，
    关系首尾相连、不与别的路径展平合并；path_id 由序列确定性生成。"""
    suggest_rows = [
        {"id": "rule:fake_thickness_to_current", "condition_code": "thickness_increase",
         "parameter_code": "welding_current", "target_change": "increase",
         "confidence": 0.8, "source_refs": ["SYNTHETIC:fake:rule_current"]},
    ]
    chains = [
        {"node_codes": ["mechanism:fake_heat", "quality:fake_penetration"],
         "rels": [
             {"id": "rule:fake_cur_heat", "confidence": 0.9, "source_refs": ["SYNTHETIC:fake:cur_heat"]},
             {"id": "rule:fake_heat_pen", "confidence": 0.85, "source_refs": ["SYNTHETIC:fake:heat_pen"]},
         ]},
        {"node_codes": ["mechanism:fake_transfer", "quality:fake_stability"],
         "rels": [
             {"id": "rule:fake_cur_transfer", "confidence": 0.8, "source_refs": ["SYNTHETIC:fake:cur_transfer"]},
             {"id": "rule:fake_transfer_stab", "confidence": 0.75, "source_refs": ["SYNTHETIC:fake:transfer_stab"]},
         ]},
    ]
    paths = _build_knowledge_paths({"welding_current": (suggest_rows, chains)})
    assert len(paths) == 2  # 两条独立路径，不合并
    assert {p.path_id for p in paths} == {"path_01", "path_02"}
    for path in paths:
        # 首尾相连：关系数 = 节点数 - 1；首节点为条件、末节点为质量
        assert len(path.relationship_ids) == len(path.node_codes) - 1
        assert path.node_codes[0] == "thickness_increase"
        assert path.node_codes[1] == "welding_current"
        assert path.node_codes[-1].startswith("quality:")
        assert path.relationship_ids[0] == "rule:fake_thickness_to_current"
        assert path.source_refs
    # 两次构建结果一致（确定性 path_id）
    again = _build_knowledge_paths({"welding_current": (suggest_rows, chains)})
    assert [p.path_id for p in paths] == [p.path_id for p in again]
    assert [p.relationship_ids for p in paths] == [p.relationship_ids for p in again]


def test_deterministic_selection_keeps_max_confidence_paths():
    """Python 确定性选择：每参数保留置信度最高路径，并列全保留。"""
    paths = [
        AdjustmentKnowledgePath(path_id="path_01", parameter_code="welding_current",
                                node_codes=["a", "b"], relationship_ids=["r1"], confidence=0.9),
        AdjustmentKnowledgePath(path_id="path_02", parameter_code="welding_current",
                                node_codes=["a", "c"], relationship_ids=["r2"], confidence=0.7),
        AdjustmentKnowledgePath(path_id="path_03", parameter_code="welding_speed",
                                node_codes=["d", "e"], relationship_ids=["r3"], confidence=0.9),
    ]
    kept = _select_paths_deterministic(paths)
    assert kept == {"path_01", "path_03"}


# ---------------------------------------------------------------------------
# 验收 P1：数量上限
# ---------------------------------------------------------------------------


def test_top_k_and_proposal_count_limits_enforced():
    """服务层强制 1<=top_k<=5、1<=proposal_count<=3（验收 P1）。"""
    requirement = WeldingRequirement(process="GMAW", thickness_mm=6.0)
    fake = build_fake_store()
    for bad_top_k in (0, 6, 99):
        with pytest.raises(ValueError, match="top_k"):
            generate_adjustment_recommendations_sync(
                requirement, EQUIPMENT_ID, top_k=bad_top_k, proposal_count=3,
                use_llm=False, store=fake,
            )
    for bad_count in (0, 4, 10):
        with pytest.raises(ValueError, match="proposal_count"):
            generate_adjustment_recommendations_sync(
                requirement, EQUIPMENT_ID, top_k=5, proposal_count=bad_count,
                use_llm=False, store=fake,
            )


# ---------------------------------------------------------------------------
# 验收 P0/P1：FakeStore 端到端（无外部服务）
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_embedding(monkeypatch):
    """离线单元测试：嵌入服务不可用时退化为纯结构化检索（确定性）。"""
    import welding_kg.case_retriever as retriever

    monkeypatch.setattr(retriever, "embed_text", lambda text, settings: None)


def _run_fake_e2e(store, requirement=None):
    requirement = requirement or WeldingRequirement(
        process="GMAW", material="carbon_steel", thickness_mm=6.0,
        joint_type="inside_corner", position="vertical",
        wire_diameter_mm=0.8, shielding_gas="CO2_100",
    )
    return generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3,
        use_llm=False, store=store,
    )


def test_fake_store_end_to_end_deterministic_twice(no_embedding):
    """FakeStore 端到端：≥2 个候选、基准案例互异、置信度降序、
    两次运行完全一致；调整项溯源齐备且数值为步长整数倍。"""
    store = build_fake_store()
    run1 = _run_fake_e2e(store)
    run2 = _run_fake_e2e(store)
    assert len(run1) >= 2
    assert len(run1) <= 3
    assert run1 == run2  # 两次运行路径与方案顺序一致（验收 P0）
    base_ids = [p.base_case_id for p in run1]
    assert len(base_ids) == len(set(base_ids))
    scores = [p.confidence for p in run1]
    assert scores == sorted(scores, reverse=True)
    weights = load_adjustment_config()["confidence_weights"]
    base_fields = {
        "welding_current": "base_current_a",
        "arc_voltage": "base_voltage_v",
        "welding_speed": "base_speed_mm_s",
    }
    recommend_fields = {
        "welding_current": "recommended_current_a",
        "arc_voltage": "recommended_voltage_v",
        "welding_speed": "recommended_speed_mm_s",
    }
    for proposal in run1:
        assert proposal.confidence == compute_confidence(proposal.confidence_breakdown, weights)
        for adj in proposal.adjustments:
            ratio = abs(adj.quantized_delta) / adj.step
            assert abs(ratio - round(ratio)) < 1e-9
            assert adj.path_ids and adj.source_refs
            if not adj.fallback:
                assert adj.support_case_ids
            # 推荐值 = 基准值 + 量化修正量（验收 P1）
            base_value = getattr(proposal, base_fields[adj.parameter_code])
            assert getattr(proposal, recommend_fields[adj.parameter_code]) == pytest.approx(
                base_value + adj.quantized_delta, abs=1e-6
            )


def test_fake_store_e2e_path_ids_from_complete_paths(no_embedding):
    """调整项 path_ids 只来自被选中的完整路径（验收 P0）。

    用与生成阶段相同的合成图谱数据重建候选完整路径，按同一确定性规则
    选择后，调整项的 path_ids 必须恰好等于选中路径关系的并集。
    """
    from helpers import _FAKE_CHAINS, _FAKE_SUGGESTS

    store = build_fake_store()
    proposals = _run_fake_e2e(store)
    assert proposals

    suggest_by_param: dict[str, list[dict]] = {}
    for row in _FAKE_SUGGESTS:
        if row["parameter_code"] in ("welding_current", "arc_voltage", "welding_speed"):
            suggest_by_param.setdefault(row["parameter_code"], []).append(row)
    chains_by_param: dict[str, list[dict]] = {}
    for chain in _FAKE_CHAINS:
        chains_by_param.setdefault(chain["code"], []).append(chain)

    # 与生成阶段一致的候选与确定性选择
    candidates = _build_knowledge_paths({
        code: (suggest_by_param[code], chains_by_param.get(code, []))
        for code in suggest_by_param
    })
    kept = _select_paths_deterministic(candidates)
    expected_by_param: dict[str, set[str]] = {}
    for path in candidates:
        if path.path_id in kept:
            expected_by_param.setdefault(path.parameter_code, set()).update(path.relationship_ids)

    for proposal in proposals:
        for adj in proposal.adjustments:
            assert set(adj.path_ids) == expected_by_param[adj.parameter_code], (
                f"{adj.parameter_code} 的 path_ids 必须恰好等于选中完整路径的关系并集"
            )
            # 每条选中路径首尾相连（已在 test_knowledge_paths_are_complete_and_independent 验证）
            assert any(pid.startswith("rule:") for pid in adj.path_ids)


def test_sync_wrapper_does_not_use_asyncio_run(no_embedding, monkeypatch):
    """同步包装不得经过 asyncio.run：受限环境中事件循环关闭线程池可能
    阻塞无法退出（验收反馈），同步调用方（CLI/测试）应走直接线程池。"""
    import asyncio as asyncio_mod

    def boom(*args, **kwargs):
        raise AssertionError("同步包装不应调用 asyncio.run")

    monkeypatch.setattr(asyncio_mod, "run", boom)
    proposals = _run_fake_e2e(build_fake_store())
    assert len(proposals) >= 2


def test_missing_step_skips_only_that_parameter(no_embedding):
    """电流、电压有步长，速度缺步长：速度被跳过且有告警，其余参数照常生成
    （验收 P0）。"""
    store = build_fake_store(with_speed_step=False)
    warnings: list[str] = []
    requirement = WeldingRequirement(
        process="GMAW", material="carbon_steel", thickness_mm=6.0,
        joint_type="inside_corner", position="vertical",
        wire_diameter_mm=0.8, shielding_gas="CO2_100",
    )
    proposals = generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3,
        use_llm=False, warnings=warnings, store=store,
    )
    assert proposals
    adjusted = {adj.parameter_code for p in proposals for adj in p.adjustments}
    assert "welding_current" in adjusted
    assert "arc_voltage" in adjusted
    assert "welding_speed" not in adjusted  # 速度缺步长 → 被跳过
    combined = warnings + [w for p in proposals for w in p.warnings]
    assert any("焊接速度" in w and "无设备步长" in w for w in combined)
    # 速度缺步长不阻止其他参数生成（上面已断言电流电压在调整项中）


def test_extra_graph_parameter_ignored_safely(no_embedding):
    """图谱存在非本阶段参数（wire_extension 带步长）时被安全忽略，
    不产生 KeyError、不进入调整项（验收 P0）。"""
    from welding_kg.adjustment import get_adjustment_steps

    store = build_fake_store(with_extra_param=True)
    steps = get_adjustment_steps(EQUIPMENT_ID, store)
    assert set(steps) == {"welding_current", "arc_voltage", "welding_speed"}
    proposals = _run_fake_e2e(store)
    assert proposals
    for proposal in proposals:
        assert all(
            adj.parameter_code in ("welding_current", "arc_voltage", "welding_speed")
            for adj in proposal.adjustments
        )


def test_unknown_range_warns_and_reduces_equipment(no_embedding):
    """设备只有文字范围：告警「无法数值核验」并降低 equipment 分，
    不得视为完全满足（验收 P1）。"""
    store = build_fake_store(numeric_ranges=False)
    proposals = _run_fake_e2e(store)
    assert proposals
    for proposal in proposals:
        assert proposal.confidence_breakdown.equipment < 1.0
    combined = [w for p in proposals for w in p.warnings]
    assert any("数值核验" in w for w in combined)


def test_numeric_range_verified_and_violation_invalid(no_embedding):
    """数值上下限存在时完成核验：该参数不再产生未知范围告警；其余参数
    仍按未知处理（equipment < 1）。越界方案判为无效并排除。"""
    store = build_fake_store(numeric_ranges=True)
    warnings: list[str] = []
    proposals = generate_adjustment_recommendations_sync(
        WeldingRequirement(
            process="GMAW", material="carbon_steel", thickness_mm=6.0,
            joint_type="inside_corner", position="vertical",
            wire_diameter_mm=0.8, shielding_gas="CO2_100",
        ),
        EQUIPMENT_ID, top_k=5, proposal_count=3,
        use_llm=False, warnings=warnings, store=store,
    )
    assert proposals
    combined = warnings + [w for p in proposals for w in p.warnings]
    # 电流有数值范围：核验通过，不再告警；电压/速度仍未知 → 告警保留
    assert not any("焊接电流" in w and "数值核验" in w for w in combined)
    assert any("数值核验" in w for w in combined)
    # 只有部分参数完成范围核验 → equipment 不得为 1（验收 P1）
    assert all(0.0 < p.confidence_breakdown.equipment < 1.0 for p in proposals)

    # 上限收紧后：所有电流推荐值越界 → 方案判为无效被排除
    store_tight = build_fake_store(current_max_value=110)
    warnings_tight: list[str] = []
    proposals_tight = generate_adjustment_recommendations_sync(
        WeldingRequirement(
            process="GMAW", material="carbon_steel", thickness_mm=6.0,
            joint_type="inside_corner", position="vertical",
            wire_diameter_mm=0.8, shielding_gas="CO2_100",
        ),
        EQUIPMENT_ID, top_k=5, proposal_count=3,
        use_llm=False, warnings=warnings_tight, store=store_tight,
    )
    combined_tight = warnings_tight + [w for p in proposals_tight for w in p.warnings]
    assert any("上限" in w for w in combined_tight)
    # 越界方案不静默截断：剩余方案的电流推荐值必须在上限内
    for proposal in proposals_tight:
        for adj in proposal.adjustments:
            if adj.parameter_code == "welding_current":
                rec = proposal.base_current_a + adj.quantized_delta
                assert rec <= 110, "越界方案应被排除，不做静默截断"


# ---------------------------------------------------------------------------
# 验收 P1：LLM 桩验证（越界路径拒绝、数值不可变、失败退化）
# ---------------------------------------------------------------------------


def _patch_llm(monkeypatch, client):
    import welding_kg.adjustment as adjustment_mod

    monkeypatch.setattr(
        adjustment_mod, "_llm_client", lambda base_url, api_key: client
    )


def test_llm_stub_selects_only_given_paths_and_never_changes_numbers(
    no_embedding, monkeypatch,
):
    """LLM 桩返回合法路径子集 + 摘要：数值字段与无 LLM 运行完全一致，
    path_ids 收缩到被选路径（验收 P1）。"""
    store = build_fake_store()
    requirement = WeldingRequirement(
        process="GMAW", material="carbon_steel", thickness_mm=6.0,
        joint_type="inside_corner", position="vertical",
        wire_diameter_mm=0.8, shielding_gas="CO2_100",
    )
    baseline = _run_fake_e2e(store, requirement)

    _patch_llm(monkeypatch, StubLLMClient(
        json.dumps({"selected_path_ids": ["path_01"], "basis": "板厚增加，按路径一调整。"})
    ))
    llm_run = generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3,
        use_llm=True, store=store,
    )
    assert llm_run
    assert [p.proposal_id for p in llm_run] == [p.proposal_id for p in baseline]
    for got, ref in zip(llm_run, baseline):
        # 数值字段：LLM 不得改变任何由程序计算的数值
        for got_adj, ref_adj in zip(got.adjustments, ref.adjustments):
            assert got_adj.raw_delta == ref_adj.raw_delta
            assert got_adj.quantized_delta == ref_adj.quantized_delta
            assert got_adj.step == ref_adj.step
            assert got_adj.magnitude == ref_adj.magnitude
            assert got_adj.support_case_ids == ref_adj.support_case_ids
            assert set(got_adj.path_ids) <= set(ref_adj.path_ids)  # 只能收缩
        assert got.recommended_current_a == ref.recommended_current_a
        assert got.recommended_voltage_v == ref.recommended_voltage_v
        assert got.recommended_speed_mm_s == ref.recommended_speed_mm_s
        assert got.base_current_a == ref.base_current_a
    # 摘要来自 LLM（桩固定内容），置信度由程序根据被选路径重新计算
    assert any(p.basis == "板厚增加，按路径一调整。" for p in llm_run)


def test_llm_stub_invalid_path_ids_rejected(no_embedding, monkeypatch):
    """LLM 桩返回非法路径 ID：拒绝并告警，退化为确定性选择（验收 P0）。"""
    store = build_fake_store()
    _patch_llm(monkeypatch, StubLLMClient(
        json.dumps({"selected_path_ids": ["path_99", "nope"], "basis": "任意"})
    ))
    warnings: list[str] = []
    proposals = generate_adjustment_recommendations_sync(
        WeldingRequirement(
            process="GMAW", material="carbon_steel", thickness_mm=6.0,
            joint_type="inside_corner", position="vertical",
            wire_diameter_mm=0.8, shielding_gas="CO2_100",
        ),
        EQUIPMENT_ID, top_k=5, proposal_count=3,
        use_llm=True, warnings=warnings, store=store,
    )
    assert proposals
    combined = warnings + [w for p in proposals for w in p.warnings]
    assert any("拒绝" in w and "path_99" in w for w in combined)
    for proposal in proposals:
        for adj in proposal.adjustments:
            assert adj.path_ids  # 退化后仍保留完整路径溯源


def test_llm_failure_degrades_to_deterministic(no_embedding, monkeypatch):
    """LLM 抛异常：退化为确定性路径选择与程序化摘要，不抛错、数值不变。"""
    store = build_fake_store()
    requirement = WeldingRequirement(
        process="GMAW", material="carbon_steel", thickness_mm=6.0,
        joint_type="inside_corner", position="vertical",
        wire_diameter_mm=0.8, shielding_gas="CO2_100",
    )
    baseline = _run_fake_e2e(store, requirement)
    _patch_llm(monkeypatch, RaisingLLMClient())
    warnings: list[str] = []
    degraded = generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3,
        use_llm=True, warnings=warnings, store=store,
    )
    assert degraded
    combined = warnings + [w for p in degraded for w in p.warnings]
    assert any("LLM" in w for w in combined)
    for got, ref in zip(degraded, baseline):
        for got_adj, ref_adj in zip(got.adjustments, ref.adjustments):
            assert got_adj.quantized_delta == ref_adj.quantized_delta
            assert got_adj.step == ref_adj.step
        assert got.confidence == ref.confidence  # 确定性退化 → 置信度一致


# ---------------------------------------------------------------------------
# 比较器契约：位置差异衔接图谱方向规则（adjustment_generation_spec §8）
# ---------------------------------------------------------------------------


def test_compare_case_generates_position_directional_codes():
    """目标为立焊/仰焊时生成 position_to_vertical / position_to_overhead，
    衔接图谱 SUGGESTS_ADJUSTMENT 规则起点；其余位置变化保留 position_changed。"""
    from welding_kg.case_comparator import compare_case

    flat_case = make_case("c_flat", position="flat")
    diffs = compare_case(
        WeldingRequirement(process="GMAW", position="vertical"), flat_case
    )
    assert any(d.code == "position_to_vertical" for d in diffs)

    diffs = compare_case(
        WeldingRequirement(process="GMAW", position="overhead"), flat_case
    )
    assert any(d.code == "position_to_overhead" for d in diffs)

    diffs = compare_case(
        WeldingRequirement(process="GMAW", position="horizontal"), flat_case
    )
    assert any(d.code == "position_changed" for d in diffs)

    # 位置相同仍为 position_same（不改变既有契约）
    diffs = compare_case(
        WeldingRequirement(process="GMAW", position="flat"), flat_case
    )
    assert any(d.code == "position_same" for d in diffs)
