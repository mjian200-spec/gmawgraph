"""演示结果的可读化渲染（终端中文报告）。

把 DemoResult 渲染成人类友好的分层报告：需求 → 基准案例 → 差异 →
推理路径 → 结论与修正方向。结论部分利用图谱规则的目标变化方向
（target_change）、设备步长/上限与基准案例参数值，给出建设性的
修正方向与设备边界，不输出杜撰的绝对数值。
"""

from __future__ import annotations

from .models import (
    CaseRecord,
    DemoResult,
    DifferenceItem,
    PathRelation,
    ReasoningPath,
    WeldingRequirement,
)

# 参数 code → 案例记录字段（用于展示"在基准值基础上调整"）
_PARAM_TO_CASE_FIELD = {
    "welding_current": ("welding_current_a", "A"),
    "arc_voltage": ("welding_voltage_v", "V"),
    "welding_speed": ("welding_speed_mm_s", "mm/s"),
    "wire_diameter": ("wire_diameter_mm", "mm"),
}
# 变化方向的展示文案
_CHANGE_TEXT = {
    "increase": "增大 ↑",
    "decrease": "降低 ↓",
}
# 需求字段的中文标签
_REQ_LABELS = {
    "process": "工艺",
    "material": "材质",
    "thickness_mm": "板厚",
    "joint_type": "接头",
    "position": "位置",
    "wire_diameter_mm": "焊丝",
    "shielding_gas": "保护气",
}


def _req_line(requirement: WeldingRequirement) -> str:
    """把需求渲染成一行：工艺 · 材质 · 板厚5.0mm · …"""
    parts: list[str] = []
    for field, label in _REQ_LABELS.items():
        value = getattr(requirement, field)
        if value is None:
            continue
        if field == "thickness_mm":
            parts.append(f"{label} {value}mm")
        elif field == "wire_diameter_mm":
            parts.append(f"{label} {value}mm")
        else:
            parts.append(f"{label} {value}")
    if requirement.target_quality:
        parts.append(f"目标: {requirement.target_quality}")
    return " · ".join(parts)


def _case_line(case: CaseRecord) -> str:
    """把案例渲染成一行：case_id（相似分）| 关键参数。"""
    params = [f"板厚 {case.thickness_mm}mm"] if case.thickness_mm is not None else []
    if case.welding_current_a is not None:
        params.append(f"电流 {case.welding_current_a:g}A")
    if case.welding_voltage_v is not None:
        params.append(f"电压 {case.welding_voltage_v:g}V")
    if case.welding_speed_mm_s is not None:
        params.append(f"速度 {case.welding_speed_mm_s:g}mm/s")
    return " | ".join(params)


def _diff_line(diff: DifferenceItem) -> str:
    """把差异渲染成一行：板厚 3.0 → 5.0 mm（thickness_increase）。"""
    labels = {"thickness_mm": "板厚", "wire_diameter_mm": "焊丝直径",
              "material": "材质", "joint_type": "接头", "position": "位置",
              "shielding_gas": "保护气"}
    label = labels.get(diff.field, diff.field)
    if diff.change in ("increase", "decrease"):
        return f"{label} {diff.before} → {diff.after} mm（{diff.code}）"
    return f"{label} {diff.before} → {diff.after}（{diff.code}）"


# 关系类型的中文展示文案（路径分段展示用）
_REL_TYPE_CN = {
    "SUGGESTS_ADJUSTMENT": "建议调节",
    "AFFECTS": "影响",
    "DETERMINES": "决定",
    "LIMITS": "限制",
}
# 变化方向的箭头标记（标注在节点名上）
_DIR_MARK = {"increase": "↑", "decrease": "↓", "same": "→"}
# 路径分段序号
_SEG_MARKS = "①②③④⑤⑥"


def _path_chain(path: ReasoningPath) -> str:
    """渲染路径的节点链：板厚增加 → 焊接电流 → 母材受热量 → 熔深。"""
    return " → ".join(n.name or n.key for n in path.nodes)


def _node_with_dir(name: str, change: str | None) -> str:
    """节点名附带变化方向标记：焊接电流↑ / 母材受热量↓。"""
    mark = _DIR_MARK.get(change or "", "")
    return f"{name}{mark}"


def _path_segments(path: ReasoningPath) -> list[str]:
    """渲染路径每一段的原始描述：节点 →[关系类型]→ 节点 + 置信度 + 原文。

    来源强度（provenance_type）与证据溯源（source_refs）仅作为数据保留键
    （模型与 JSON 输出中仍携带，供后续溯源功能使用），不在报告中输出；
    变化方向以 ↑/↓ 标注在节点名上，保证可读性。
    """
    lines: list[str] = []
    for i, rel in enumerate(path.relations):
        # 关系 i 连接节点 i → i+1（path_retriever 保证首段 SUGGESTS_ADJUSTMENT）
        from_raw = path.nodes[i].name or path.nodes[i].key if i < len(path.nodes) else "?"
        to_raw = (
            path.nodes[i + 1].name or path.nodes[i + 1].key
            if i + 1 < len(path.nodes)
            else "?"
        )
        from_node = _node_with_dir(from_raw, rel.source_change)
        to_node = _node_with_dir(to_raw, rel.target_change)
        mark = _SEG_MARKS[i] if i < len(_SEG_MARKS) else f"[{i + 1}]"
        rel_cn = _REL_TYPE_CN.get(rel.type, rel.type)
        conf = f"（置信度 {rel.confidence:.2f}）" if rel.confidence is not None else ""
        lines.append(f"     {mark} {from_node} →[{rel_cn}]→ {to_node}{conf}")
        if rel.condition_text:
            lines.append(f"        {rel.condition_text}")
    return lines


def _equipment_notes(path: ReasoningPath, parameter_code: str) -> list[str]:
    """收集路径上该参数相关的设备约束描述（步长/设定方式/范围规则）。"""
    notes: list[str] = []
    for limit in path.equipment_limits:
        props = limit.get("properties", {})
        equipment_id = limit.get("equipment_id", "")
        # 只展示与当前结论参数相关的约束（路径上的第一个参数）
        # equipment_limits 在 path_retriever 中按参数 code 分组附加，
        # 此处直接按属性内容渲染
        if "adjustment_step" in props:
            range_rule = f"，受{props['range_rule']}约束" if props.get("range_rule") else ""
            notes.append(
                f"设备 {equipment_id} 每次微调 {props['adjustment_step']:g}"
                f"{props.get('unit', '')}{range_rule}"
            )
        elif props.get("supports_direct_setting"):
            notes.append(
                f"设备 {equipment_id} 可经 {props.get('setting_method', '指令')} 直接给定"
            )
    # 去重并去噪
    seen: set[str] = set()
    unique = []
    for note in notes:
        if note not in seen:
            seen.add(note)
            unique.append(note)
    return unique


def _conclusion_item(path: ReasoningPath, base_case: CaseRecord) -> dict | None:
    """从一条选中路径提取结论要点（参数、方向、基准值、规则文本、设备边界）。"""
    if not path.relations:
        return None
    first_rel = path.relations[0]  # 首段必为 SUGGESTS_ADJUSTMENT（规范 §9）
    change = first_rel.target_change or ""
    # 首段终点即建议调节的参数
    param_node = path.nodes[1] if len(path.nodes) > 1 else None
    parameter_code = param_node.key if param_node else ""
    param_name = param_node.name or parameter_code if param_node else ""
    direction = _CHANGE_TEXT.get(change, change)

    # 基准案例中该参数的记录值（如有）
    base_text = ""
    mapping = _PARAM_TO_CASE_FIELD.get(parameter_code)
    if mapping:
        field_name, unit = mapping
        base_value = getattr(base_case, field_name)
        if base_value is not None:
            base_text = f"基准案例 {base_case.case_id} 记录值 {base_value:g}{unit}"
    return {
        "parameter_name": param_name,
        "parameter_code": parameter_code,
        "direction": direction,
        "base_text": base_text,
        "rule_text": first_rel.condition_text or "",
        "confidence": first_rel.confidence,
        "source_refs": first_rel.source_refs,
        "equipment_notes": _equipment_notes(path, parameter_code),
    }


def format_demo_result(requirement: WeldingRequirement, result: DemoResult) -> str:
    """把演示结果渲染为终端中文报告（默认输出模式）。"""
    lines: list[str] = []
    bar = "═" * 42
    lines.append(f"╔{bar}╗")
    lines.append("║        GMAWGraph 推理演示报告        ║")
    lines.append(f"╚{bar}╝")

    # 1) 需求
    lines.append("\n【焊接需求】")
    lines.append(f"  {_req_line(requirement)}")

    # 2) 检索结果与基准案例
    if result.base_case is None:
        lines.append("\n【检索结果】")
        lines.append("  未找到候选案例")
        for w in result.warnings:
            lines.append(f"  ⚠ {w}")
        return "\n".join(lines)

    top1 = result.case_matches[0]
    lines.append("\n【基准案例】")
    lines.append(
        f"  {result.base_case.case_id}（相似分 {top1.total_score:.3f}，"
        f"结构 {top1.structured_score:.3f} / 语义 {top1.semantic_score:.3f}）"
    )
    lines.append(f"  {_case_line(result.base_case)}")
    if result.base_case.retrieval_text:
        lines.append(f"  记录: {result.base_case.retrieval_text}")
    # 其余候选用一行带过
    if len(result.case_matches) > 1:
        others = "  ".join(
            f"{m.case.case_id}({m.total_score:.3f})"
            for m in result.case_matches[1:4]
        )
        lines.append(f"  其他候选: {others}")

    # 3) 差异
    non_same = [d for d in result.differences if d.change != "same"]
    lines.append("\n【差异】")
    if non_same:
        for d in non_same:
            lines.append(f"  · {_diff_line(d)}")
    else:
        lines.append("  · 需求与基准案例完全一致，无差异")

    # 4) 推理路径
    lines.append("\n【推理路径】")
    if not result.candidate_paths:
        lines.append("  未查询到推理路径")
    else:
        selected = {p.path_id: p for p in result.candidate_paths
                    if p.path_id in result.selected_path_ids}
        if selected:
            lines.append(
                f"  候选 {len(result.candidate_paths)} 条，LLM 选中 {len(selected)} 条"
                + (f"：{result.selection_reason}" if result.selection_reason else "")
            )
            for i, path in enumerate(selected.values(), 1):
                lines.append(f"  {i}) {_path_chain(path)}")
                lines.extend(_path_segments(path))  # 每段关系的原文描述与证据
        else:
            lines.append(
                f"  候选 {len(result.candidate_paths)} 条，LLM 未做出选择，全部列出："
            )
            for i, path in enumerate(result.candidate_paths[:5], 1):
                lines.append(f"  {i}) {_path_chain(path)}")

    # 5) 结论与修正方向（建设性结论）
    if selected:
        lines.append("\n【结论与修正方向】")
        seen_params: set[str] = set()
        for path in selected.values():
            item = _conclusion_item(path, result.base_case)
            if item is None or item["parameter_code"] in seen_params:
                continue  # 同一参数多条路径只给一次结论
            seen_params.add(item["parameter_code"])
            direction = item["direction"]
            prefix = f"建议在{item['base_text']} 基础上" if item["base_text"] else "建议"
            conf = f"（置信度 {item['confidence']:.2f}）" if item["confidence"] is not None else ""
            lines.append(f"  ■ {item['parameter_name']}：{prefix} {direction}{conf}")
            # 规则依据：只输出规则描述文本；证据溯源（source_refs）保留在
            # 数据字段中待用，不在报告中输出
            if item["rule_text"]:
                lines.append(f"    规则依据: {item['rule_text']}")
            for note in item["equipment_notes"]:
                lines.append(f"    设备边界: {note}")
        lines.append("  ※ 以上为知识图谱规则给出的调整方向与设备边界，具体修正量请结合实际施焊验证。")

    # 6) 告警
    if result.warnings:
        lines.append("\n【告警】")
        for w in result.warnings:
            lines.append(f"  ⚠ {w}")
    return "\n".join(lines)
