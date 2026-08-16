"""修正量生成集成测试（验收规范 P1：真实 Neo4j/BGE/LLM 验收，不得跳过）。

Neo4j 不可达时直接失败（pytest.fail），而不是静默 skip——正式验收必须
执行本组测试。运行方式：

    /ENV/Anaconda/envs/jm/GMAWGraph/bin/python -m pytest -q -m integration
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import load_example_requirement
from welding_kg.adjustment import (
    compute_confidence,
    generate_adjustment_recommendations_sync,
    get_adjustment_steps,
)
from welding_kg.graph_importer import import_case_json, import_graph_json
from welding_kg.neo4j_store import Neo4jStore
from welding_kg.path_retriever import get_path_sources, query_parameter_chains
from welding_kg.settings import Settings, load_adjustment_config

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = PROJECT_ROOT / "data" / "seed"
EQUIPMENT_ID = "crobotpos_arc_module"


@pytest.fixture(scope="module")
def store() -> Neo4jStore:
    """初始化模式并幂等导入图谱/案例种子；Neo4j 不可达时直接失败。"""
    st = Neo4jStore(Settings.from_env())
    try:
        st.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        st.close()
        pytest.fail(
            f"Neo4j 不可达，集成测试不得跳过（验收规范 P1）。"
            f"请先启动服务并导入数据后执行 -m integration：{exc}"
        )
    st.initialize_schema()
    graph_result = import_graph_json(str(SEED_DIR / "graph_seed.json"), st)
    assert not graph_result.errors, "; ".join(graph_result.errors)
    case_result = import_case_json(str(SEED_DIR / "cases.json"), st)
    assert not case_result.errors, "; ".join(case_result.errors)
    yield st
    st.close()


# ---------------------------------------------------------------------------
# 规范 §16.1—3：步长从图谱读取
# ---------------------------------------------------------------------------


def test_adjustment_steps_read_from_graph(store):
    """电流 5 A / 电压 0.2 V 来自说明书（manual），速度 0.5 mm/s 来自
    图谱默认步长（project_default），均不在代码中（规范 §4）。"""
    steps = get_adjustment_steps(EQUIPMENT_ID, store)
    assert set(steps) == {"welding_current", "arc_voltage", "welding_speed"}

    current = steps["welding_current"]
    assert current.step == 5.0 and current.source_type == "manual"
    assert current.unit == "A" and current.source_refs

    voltage = steps["arc_voltage"]
    assert voltage.step == 0.2 and voltage.source_type == "manual"
    assert voltage.unit == "V" and voltage.source_refs

    speed = steps["welding_speed"]
    assert speed.step == 0.5 and speed.source_type == "project_default"
    assert speed.unit == "mm/s"


# ---------------------------------------------------------------------------
# 验收 P0：真实图谱上的完整、独立知识路径
# ---------------------------------------------------------------------------


def test_knowledge_chains_preserved_independently_on_real_graph(store):
    """真实图谱：每条 Parameter→Mechanism→Quality 链独立返回，
    关系首尾相连，不被展平合并（验收 P0）。"""
    chains = query_parameter_chains(store, ["welding_current"])
    assert len(chains["welding_current"]) >= 2, "焊接电流应有多条独立推理链"
    seen_node_sequences: set[tuple] = set()
    for chain in chains["welding_current"]:
        assert len(chain["rels"]) == len(chain["node_codes"]) - 1
        assert chain["node_codes"][0] == "welding_current"
        seq = tuple(chain["node_codes"])
        assert seq not in seen_node_sequences, "链按节点序列去重"
        seen_node_sequences.add(seq)
        assert all(rel.get("id") for rel in chain["rels"])


# ---------------------------------------------------------------------------
# 规范 §16.8/§16.9：并行生成 + 结果顺序可重复
# ---------------------------------------------------------------------------


def test_parallel_proposals_deterministic(store):
    """3 个不同基准案例并行产生候选；两次运行结果完全一致（规范 §12）。"""
    requirement = load_example_requirement()
    run1 = generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3, use_llm=False
    )
    run2 = generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3, use_llm=False
    )
    assert len(run1) >= 2, "至少输出 2 个有效候选（规范 §17）"
    assert len(run1) <= 3, "最多 3 个候选（验收 P1）"
    assert run1 == run2, "两次生成结果必须完全一致（含依据摘要与置信度）"
    base_ids = [p.base_case_id for p in run1]
    assert len(base_ids) == len(set(base_ids)), "基准案例必须互不相同"
    scores = [p.confidence for p in run1]
    assert scores == sorted(scores, reverse=True), "最终按程序置信度排序（规范 §13）"
    assert run1[0].proposal_id  # 排序后首项即最高分方案


# ---------------------------------------------------------------------------
# 规范 §16.7/§16.11/§17：端到端溯源与量化
# ---------------------------------------------------------------------------


def test_end_to_end_adjustments_quantized_and_traceable(store):
    """每项修正：方向来自图谱、幅度为步长整数倍、案例/路径/来源引用齐备、
    推荐值=基准值+量化修正量、置信度总分等于配置加权和。"""
    requirement = load_example_requirement()
    proposals = generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3, use_llm=False
    )
    assert len(proposals) >= 2
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
    for proposal in proposals:
        assert proposal.base_case_id and proposal.proposal_id
        assert proposal.basis
        assert proposal.confidence == compute_confidence(proposal.confidence_breakdown, weights)
        assert proposal.confidence_breakdown.consensus >= 0
        assert proposal.confidence_breakdown.case_support >= 0
        for adj in proposal.adjustments:
            ratio = abs(adj.quantized_delta) / adj.step
            assert abs(ratio - round(ratio)) < 1e-9
            assert adj.path_ids, f"{adj.parameter_code} 缺少图谱路径 id"
            assert adj.source_refs, f"{adj.parameter_code} 缺少来源引用"
            if not adj.fallback:
                assert adj.support_case_ids, f"{adj.parameter_code} 非退化修正缺少支持案例"
            assert getattr(proposal, recommend_fields[adj.parameter_code]) == pytest.approx(
                getattr(proposal, base_fields[adj.parameter_code]) + adj.quantized_delta,
                abs=1e-6,
            )
    # 无 LLM 时未知范围告警与 equipment 降分（真实种子只有文字 range_rule）
    combined = [w for p in proposals for w in p.warnings]
    assert any("数值核验" in w for w in combined)
    assert all(p.confidence_breakdown.equipment < 1.0 for p in proposals)


def test_path_ids_exist_in_graph(store):
    """溯源路径 id 必须是图谱中真实存在的关系（规范 §14.3）。"""
    requirement = load_example_requirement()
    proposals = generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3, use_llm=False
    )
    all_ids = sorted({pid for p in proposals for a in p.adjustments for pid in a.path_ids})
    assert all_ids
    sources = get_path_sources(all_ids, store)
    assert set(sources) == set(all_ids), "所有 path_ids 均能在图谱中查到来源"


def test_missing_equipment_step_returns_warning(store):
    """设备无步长定义时停止生成并返回告警（规范 §4.3/§16.13）。"""
    requirement = load_example_requirement()
    warnings: list[str] = []
    proposals = generate_adjustment_recommendations_sync(
        requirement, "equipment:nonexistent", top_k=5, proposal_count=3,
        use_llm=False, warnings=warnings,
    )
    assert proposals == []
    assert any("步长" in w for w in warnings)


def test_unified_mode_skips_voltage_in_proposals(store):
    """一元模式下方案不给出独立电压推荐值（规范 §10/§16.12）。"""
    requirement = load_example_requirement()
    requirement.control_mode = "unified"
    proposals = generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3, use_llm=False
    )
    assert proposals
    for proposal in proposals:
        assert proposal.recommended_voltage_v is None, "一元模式不得按独立伏特值修正电压"


def test_llm_selection_real_or_degraded(store):
    """真实 LLM：依据摘要非空、path_ids 全部存在于图谱；LLM 不可用时
    自动退化为确定性选择与程序化摘要（不抛错）。"""
    requirement = load_example_requirement()
    proposals = generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3, use_llm=True
    )
    assert proposals
    all_ids = sorted({pid for p in proposals for a in p.adjustments for pid in a.path_ids})
    assert all_ids
    sources = get_path_sources(all_ids, store)
    assert set(sources) == set(all_ids), "LLM 选择后的 path_ids 必须全部存在于图谱"
    for proposal in proposals:
        assert proposal.basis, "依据摘要不得为空（LLM 失败时应退化为程序化摘要）"
        for adj in proposal.adjustments:
            # LLM 不得改变由程序计算的数值：推荐值一致性仍成立
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
            assert getattr(proposal, recommend_fields[adj.parameter_code]) == pytest.approx(
                getattr(proposal, base_fields[adj.parameter_code]) + adj.quantized_delta,
                abs=1e-6,
            )


def test_selected_proposal_is_highest_scoring(store):
    """排序后首项即最高分方案；selected_proposal_id 必须等于排序后首项。"""
    requirement = load_example_requirement()
    proposals = generate_adjustment_recommendations_sync(
        requirement, EQUIPMENT_ID, top_k=5, proposal_count=3, use_llm=False
    )
    assert proposals
    top = proposals[0]
    assert top.confidence == max(p.confidence for p in proposals)
    result = {
        "requirement_id": requirement.requirement_id,
        "equipment_id": EQUIPMENT_ID,
        "proposals": proposals,
        "selected_proposal_id": top.proposal_id,
        "warnings": [],
    }
    assert result["selected_proposal_id"] == top.proposal_id
    assert all(p.confidence <= top.confidence for p in proposals[1:])
