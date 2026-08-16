"""需求与案例差异计算（规范 §9 案例差异层）。

差异由程序计算，不使用 LLM：数值变化为 increase/decrease/same，
类别变化为 changed/same；差异代码格式为 {field}_{change}。
位置差异衔接图谱方向规则（adjustment_generation_spec §8）：
目标为立焊/仰焊时生成 position_to_vertical / position_to_overhead，
直接衔接 condition:position_to_* 的 SUGGESTS_ADJUSTMENT 规则起点。
"""

from __future__ import annotations

from .models import CaseRecord, DifferenceItem, WeldingRequirement
from .settings import load_retrieval_config

# 差异代码中的字段别名：模型字段名 → 代码字段名。
# 规范 §9 示例为 thickness_increase，故数值型字段省略 _mm 后缀，
# 使差异代码更贴近工艺习惯（Condition 节点 code 需与之一致）。
_FIELD_CODE_ALIAS = {
    "thickness_mm": "thickness",
    "wire_diameter_mm": "wire_diameter",
}

# 位置差异 → 图谱方向条件（adjustment_generation_spec §8）：
# 目标改为立焊 → position_to_vertical；目标改为仰焊 → position_to_overhead。
# 其余位置变化（横焊/平焊/船型）在图谱中无对应规则，保留 position_changed。
_POSITION_CHANGE_CODES = {
    "vertical": "position_to_vertical",
    "overhead": "position_to_overhead",
}


def _diff_code(field: str, change: str) -> str:
    """生成差异代码 {字段别名}_{变化}，如 thickness_increase。"""
    return f"{_FIELD_CODE_ALIAS.get(field, field)}_{change}"


def compare_case(
    target: WeldingRequirement | CaseRecord, case: CaseRecord
) -> list[DifferenceItem]:
    """逐字段对比目标工况与基准案例，生成差异列表（规范 §9）。

    目标通常是焊接需求，也允许是另一个案例（修正量生成时比较支持案例
    与基准案例，adjustment_generation_spec §9）。
    案例缺失的字段视为 changed 并附说明；仅对比检索配置中的结构化字段。
    """
    cfg = load_retrieval_config()
    fields_cfg: dict = cfg["structured_fields"]
    differences: list[DifferenceItem] = []

    for field, field_cfg in fields_cfg.items():
        req_value = getattr(target, field)
        case_value = getattr(case, field)
        if req_value is None:
            continue  # 目标未给出该字段则不产生差异

        if case_value is None:
            # 案例缺失该字段：视为类别变化，附说明
            differences.append(
                DifferenceItem(
                    field=field,
                    change="changed",
                    code=_diff_code(field, "changed"),
                    before="",
                    after=str(req_value),
                    note="案例未提供该字段",
                )
            )
            continue

        if field_cfg.get("type") == "numeric":
            a, b = float(req_value), float(case_value)
            if abs(a - b) < 1e-9:
                change = "same"
            else:
                change = "increase" if a > b else "decrease"
            code = _diff_code(field, change)
        else:  # categorical
            change = "same" if str(req_value) == str(case_value) else "changed"
            code = _diff_code(field, change)
            # 位置差异生成方向性代码，衔接图谱规则起点（§8）：
            # 目标为立焊/仰焊时替代 position_changed
            if field == "position" and change == "changed":
                directional = _POSITION_CHANGE_CODES.get(str(req_value))
                if directional:
                    code = directional

        differences.append(
            DifferenceItem(
                field=field,
                change=change,
                code=code,
                before=str(case_value),
                after=str(req_value),
            )
        )
    return differences
