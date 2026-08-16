"""单元测试公共构件：FakeStore、LLM 桩、合成案例与合成图谱行。

全部为 SYNTHETIC 工程测试数据，不来自任何书籍或真实工艺案例，
仅用于验证代码路径（验收规范 P1：单元测试不依赖外部服务）。
"""

from __future__ import annotations

import json
from pathlib import Path

from welding_kg.models import CaseMatch, CaseRecord, WeldingRequirement

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeStore:
    """按查询子串顺序匹配的假存储（不接触真实 Neo4j）。

    patterns: [(子串, 行列表, 可选过滤函数)]，按注册顺序匹配，
    过滤函数 (query, params, rows) -> rows 模拟 Cypher 的 WHERE 过滤。
    """

    def __init__(self, patterns: list[tuple] | None = None):
        self._patterns = list(patterns or [])

    def execute_read(self, query: str, params: dict) -> list[dict]:
        for marker, rows, *rest in self._patterns:
            if marker in query:
                if rest and rest[0] is not None:
                    return [dict(r) for r in rest[0](query, params, rows)]
                return [dict(r) for r in rows]
        raise AssertionError(f"FakeStore 未覆盖查询：{query[:120]}")

    def close(self) -> None:
        """与 Neo4jStore 接口对齐（无资源可释放）。"""


# ---------------------------------------------------------------------------
# 合成案例与需求
# ---------------------------------------------------------------------------


def make_case(
    case_id: str,
    *,
    material: str = "carbon_steel",
    thickness: float = 3.0,
    joint: str = "inside_corner",
    position: str = "vertical",
    wire: float = 0.8,
    gas: str = "CO2_100",
    current: float | None = 100.0,
    voltage: float | None = 15.0,
    speed: float | None = 8.0,
    process: str = "GMAW",
) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        process=process,
        material=material,
        thickness_mm=thickness,
        joint_type=joint,
        position=position,
        wire_diameter_mm=wire,
        shielding_gas=gas,
        welding_current_a=current,
        welding_voltage_v=voltage,
        welding_speed_mm_s=speed,
        retrieval_text="",
    )


def make_match(case: CaseRecord, score: float) -> CaseMatch:
    return CaseMatch(
        case=case,
        total_score=score,
        structured_score=score,
        semantic_score=0.0,
        field_scores={},
    )


def load_example_requirement() -> WeldingRequirement:
    return WeldingRequirement.model_validate(
        json.loads((PROJECT_ROOT / "data" / "example_requirement.json").read_text(encoding="utf-8"))
    )


# ---------------------------------------------------------------------------
# 合成图谱行（SYNTHETIC，仅供单元测试）
# ---------------------------------------------------------------------------

_FAKE_CASES = [
    make_case("fake_c_s1", thickness=5.0, current=120.0, voltage=16.0, speed=7.0),
    make_case("fake_c_s2", thickness=4.0, current=115.0, voltage=15.8, speed=7.5),
    make_case("fake_c_a", thickness=3.0, current=100.0, voltage=15.0, speed=8.0),
    make_case("fake_c_b", thickness=1.0, current=90.0, voltage=14.6, speed=9.0),
    make_case("fake_c_c", thickness=2.0, joint="lap", current=95.0, voltage=14.8, speed=8.5),
]

_FAKE_SUGGESTS = [
    {
        "id": "rule:fake_thickness_to_current",
        "condition_code": "thickness_increase",
        "parameter_code": "welding_current",
        "target_change": "increase",
        "confidence": 0.8,
        "condition_text": "合成测试：板厚增加→电流增加",
        "provenance_type": "synthetic",
        "source_refs": ["SYNTHETIC:fake:rule_current"],
    },
    {
        "id": "rule:fake_thickness_to_voltage",
        "condition_code": "thickness_increase",
        "parameter_code": "arc_voltage",
        "target_change": "increase",
        "confidence": 0.75,
        "condition_text": "合成测试：板厚增加→电压增加",
        "provenance_type": "synthetic",
        "source_refs": ["SYNTHETIC:fake:rule_voltage"],
    },
    {
        "id": "rule:fake_thickness_to_speed",
        "condition_code": "thickness_increase",
        "parameter_code": "welding_speed",
        "target_change": "decrease",
        "confidence": 0.7,
        "condition_text": "合成测试：板厚增加→速度降低",
        "provenance_type": "synthetic",
        "source_refs": ["SYNTHETIC:fake:rule_speed"],
    },
    # 非本阶段参数：真实图谱查询会按白名单过滤，FakeStore 亦模拟过滤
    {
        "id": "rule:fake_thickness_to_extension",
        "condition_code": "thickness_increase",
        "parameter_code": "wire_extension",
        "target_change": "increase",
        "confidence": 0.6,
        "condition_text": "合成测试：非本阶段参数",
        "provenance_type": "synthetic",
        "source_refs": ["SYNTHETIC:fake:rule_extension"],
    },
]

_FAKE_CHAINS = [
    # 焊接电流的两条独立推理链（验证路径不展平合并）
    {
        "code": "welding_current",
        "node_codes": ["welding_current", "mechanism:fake_heat", "quality:fake_penetration"],
        "rels": [
            {"id": "rule:fake_cur_heat", "confidence": 0.9, "source_refs": ["SYNTHETIC:fake:cur_heat"]},
            {"id": "rule:fake_heat_pen", "confidence": 0.85, "source_refs": ["SYNTHETIC:fake:heat_pen"]},
        ],
    },
    {
        "code": "welding_current",
        "node_codes": ["welding_current", "mechanism:fake_transfer", "quality:fake_stability"],
        "rels": [
            {"id": "rule:fake_cur_transfer", "confidence": 0.8, "source_refs": ["SYNTHETIC:fake:cur_transfer"]},
            {"id": "rule:fake_transfer_stab", "confidence": 0.75, "source_refs": ["SYNTHETIC:fake:transfer_stab"]},
        ],
    },
    {
        "code": "arc_voltage",
        "node_codes": ["arc_voltage", "mechanism:fake_arc_len", "quality:fake_width"],
        "rels": [
            {"id": "rule:fake_vol_arc", "confidence": 0.88, "source_refs": ["SYNTHETIC:fake:vol_arc"]},
            {"id": "rule:fake_arc_width", "confidence": 0.82, "source_refs": ["SYNTHETIC:fake:arc_width"]},
        ],
    },
    {
        "code": "welding_speed",
        "node_codes": ["welding_speed", "mechanism:fake_heat_density", "quality:fake_penetration"],
        "rels": [
            {"id": "rule:fake_speed_heat", "confidence": 0.86, "source_refs": ["SYNTHETIC:fake:speed_heat"]},
            {"id": "rule:fake_heat_pen2", "confidence": 0.84, "source_refs": ["SYNTHETIC:fake:heat_pen2"]},
        ],
    },
]


def build_fake_store(
    *,
    with_speed_step: bool = True,
    with_extra_param: bool = True,
    voltage_unified_rule: bool = False,
    numeric_ranges: bool = False,
    current_max_value: float | None = None,
) -> FakeStore:
    """按测试场景组装合成图谱 FakeStore（全部为 SYNTHETIC 行）。"""
    current_props: dict = {
        "adjustment_step": 5.0,
        "unit": "A",
        "range_rule": "电流曲线匹配范围",
        "source_refs": ["SYNTHETIC:fake:limit_current"],
    }
    if numeric_ranges:
        current_props.update({"min_value": 50, "max_value": 400})
    if current_max_value is not None:
        current_props.update({"min_value": 50, "max_value": current_max_value})
    limits: list[dict] = [
        {"code": "welding_current", "equipment_id": "fake_welder", "props": current_props},
        {
            "code": "arc_voltage",
            "equipment_id": "fake_welder",
            "props": {
                "adjustment_step": 0.2,
                "unit": "V",
                "range_rule": "电压曲线匹配范围",
                **(
                    {"unified_mode_rule": "实际焊接电压由焊接电流匹配，机器人设置值为百分比增益"}
                    if voltage_unified_rule else {}
                ),
                "source_refs": ["SYNTHETIC:fake:limit_voltage"],
            },
        },
    ]
    if with_speed_step:
        limits.append({
            "code": "welding_speed",
            "equipment_id": "fake_welder",
            "props": {"default_step": 0.5, "unit": "mm/s", "source_refs": ["SYNTHETIC:fake:limit_speed"]},
        })
    if with_extra_param:
        limits.append({
            "code": "wire_extension",
            "equipment_id": "fake_welder",
            "props": {"default_step": 1.0, "unit": "mm", "source_refs": ["SYNTHETIC:fake:limit_extension"]},
        })

    def filter_suggests(_query, params, rows):
        cc = set(params.get("cc") or [])
        pc = set(params.get("pc") or [])
        return [r for r in rows if r["condition_code"] in cc and r["parameter_code"] in pc]

    def filter_limits(_query, params, rows):
        eid = params.get("eid")
        codes = params.get("codes")
        return [
            r for r in rows
            if (eid is None or r["equipment_id"] == eid)
            and (codes is None or r["code"] in codes)
        ]

    def filter_chains(_query, params, rows):
        codes = set(params.get("codes") or [])
        return [r for r in rows if r["code"] in codes]

    def filter_cases(_query, params, rows):
        process = params.get("p")
        return [
            {"p": c.model_dump(exclude={"conditions", "parameters", "results"})}
            for c in _FAKE_CASES
            if c.process == process
        ]

    def filter_path_sources(_query, params, rows):
        ids = set(params.get("ids") or [])
        return [r for r in rows if r["id"] in ids]

    all_rel_ids = [r["id"] for r in _FAKE_SUGGESTS]
    for chain in _FAKE_CHAINS:
        all_rel_ids.extend(rel["id"] for rel in chain["rels"])
    path_sources = [
        {"id": rel_id, "source_refs": [f"SYNTHETIC:fake:{rel_id}"]}
        for rel_id in sorted(set(all_rel_ids))
    ]

    return FakeStore([
        ("HAS_CONDITION", []),  # 案例概念引用回填：合成数据无引用
        ("MATCH (n:Case", [], filter_cases),
        ("SUGGESTS_ADJUSTMENT", _FAKE_SUGGESTS, filter_suggests),
        ("LIMITS", limits, filter_limits),
        ("MATCH p = (pa:Parameter)", _FAKE_CHAINS, filter_chains),
        ("r.id IN", path_sources, filter_path_sources),
    ])


# ---------------------------------------------------------------------------
# LLM 桩（验证越界路径拒绝与数值不可变性，不依赖真实 LLM 可用性）
# ---------------------------------------------------------------------------


class _StubMessage:
    def __init__(self, content: str):
        self.content = content


class _StubChoice:
    def __init__(self, content: str):
        self.message = _StubMessage(content)


class _StubResponse:
    def __init__(self, content: str):
        self.choices = [_StubChoice(content)]


class _StubCompletions:
    def __init__(self, content: str):
        self._content = content

    def create(self, **kwargs):
        return _StubResponse(self._content)


class _StubChat:
    def __init__(self, content: str):
        self.completions = _StubCompletions(content)


class StubLLMClient:
    """返回固定 JSON 内容的 LLM 客户端桩。"""

    def __init__(self, content: str):
        self.chat = _StubChat(content)


class RaisingLLMClient:
    """总是抛异常的 LLM 客户端桩（验证调用失败时的确定性退化）。"""

    @property
    def chat(self):
        raise ConnectionError("SYNTHETIC: LLM 不可用")
