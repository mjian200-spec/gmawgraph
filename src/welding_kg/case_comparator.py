"""需求与案例差异计算（规范 §9 案例差异层）。

差异由程序计算，不使用 LLM：数值变化为 increase/decrease/same，
类别变化为 changed/same；差异代码格式为 {field}_{change}。
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


def _diff_code(field: str, change: str) -> str:
    """生成差异代码 {字段别名}_{变化}，如 thickness_increase。"""
    return f"{_FIELD_CODE_ALIAS.get(field, field)}_{change}"


def compare_case(
    requirement: WeldingRequirement, case: CaseRecord
) -> list[DifferenceItem]:
    """逐字段对比需求与案例，生成差异列表（规范 §9）。

    案例缺失的字段视为 changed 并附说明；仅对比检索配置中的结构化字段。
    """
    cfg = load_retrieval_config()
    fields_cfg: dict = cfg["structured_fields"]
    differences: list[DifferenceItem] = []

    for field, field_cfg in fields_cfg.items():
        req_value = getattr(requirement, field)
        case_value = getattr(case, field)
        if req_value is None:
            continue  # 需求未给出该字段则不产生差异

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
        else:  # categorical
            change = "same" if str(req_value) == str(case_value) else "changed"

        differences.append(
            DifferenceItem(
                field=field,
                change=change,
                code=_diff_code(field, change),
                before=str(case_value),
                after=str(req_value),
            )
        )
    return differences
