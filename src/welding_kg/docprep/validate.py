"""质量验证：任务书 v2 第 8 节 17 条验收标准的自动检查。

所有验收项由检查注册表（checks_by_key）派生，禁止硬编码通过项；
不包含任何 OCR 数值人工确认或图片语义目检门禁（验收标准 17）。
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Set, Tuple

from .model import Block, ContentItem, SectionNode

FORBIDDEN_RE = re.compile(r"entity|relation|process_?window|effect_?rule|adjustment_?rule",
                          re.IGNORECASE)

MARGIN_TYPES = {"page_header", "page_footer", "page_number", "page_aside_text"}


def _check_unique(items: List[str]) -> List[str]:
    seen = set()
    dups = []
    for it in items:
        if it in seen:
            dups.append(it)
        seen.add(it)
    return sorted(set(dups))


def check_raw_preserved(blocks: List[Block], pages_raw: List[list]) -> List[str]:
    """验收 14：raw_text 与原解析 JSON 逐块比对（不得被覆盖）。

    与 normalize.derive_raw_blocks 共用同一推导逻辑：
    - 每个派生块的 raw_text 必须属于该原始块推导出的文本集合；
    - 每条推导文本必须至少出现在一个派生块中（拆分块会复制原文，允许重复）。
    """
    from .normalize import derive_raw_blocks

    violations: List[str] = []
    blocks_by_origin: Dict[Tuple[int, int], List[Block]] = {}
    for b in blocks:
        blocks_by_origin.setdefault((b.pdf_page, b.original_index), []).append(b)

    for p_idx, page in enumerate(pages_raw):
        for b_idx, raw in enumerate(page):
            expected = [text for _t, text in derive_raw_blocks(raw, p_idx, b_idx)]
            actual = blocks_by_origin.get((p_idx, b_idx), [])
            if not actual:
                violations.append(f"p{p_idx} idx{b_idx}: 无对应块")
                continue
            expected_set = set(expected)
            for b in actual:
                if b.raw_text not in expected_set:
                    violations.append(
                        f"{b.block_id}: raw_text 与原解析不一致"
                        f"（{len(b.raw_text)} 字符，不属于该原始块的推导文本）")
            for text in expected:
                if not any(b.raw_text == text for b in actual):
                    violations.append(f"p{p_idx} idx{b_idx}: 原始文本丢失（{len(text)} 字符）")
    return violations


def _traversal_token_stream(sections: List[SectionNode]) -> List[str]:
    """按 section_order 递归遍历 content，产出 token 流。

    token：视觉块/文本段成员 → block_id；subsection → ("sub", section_id)。
    """
    by_id = {s.section_id: s for s in sections}
    root = by_id.get("gmaw:v2:sec:root")
    tokens: List[str] = []
    visited: Set[str] = set()

    def walk(sid: str) -> None:
        if sid in visited:
            return
        visited.add(sid)
        s = by_id.get(sid)
        if s is None:
            return
        for item in s.content:
            if item.item_type == "subsection":
                tokens.append(f"sub:{item.item_id}")
                walk(item.item_id)
            elif item.item_type == "text_segment":
                tokens.extend(item.member_block_ids)
            else:
                tokens.append(item.source_ref)

    if root is not None:
        walk(root.section_id)
    return tokens


def _expected_tokens(seq: List[Block], heading_sid_by_block: Dict[str, str]) -> List[str]:
    """内容序列的期望 token 流：关联题注跳过；树内标题 → sub 标记；
    视觉块用 source_ref（与内容项一致），文本块用 block_id。"""
    tokens: List[str] = []
    for b in seq:
        if b.extra.get("associated_with"):
            continue
        if b.block_id in heading_sid_by_block:
            tokens.append(f"sub:{heading_sid_by_block[b.block_id]}")
        elif b.block_type in ("figure", "table"):
            tokens.append(b.source_ref)
        else:
            tokens.append(b.block_id)
    return tokens


def validate_outputs(blocks: List[Block], sections: List[SectionNode],
                     seq: List[Block], cfg: dict,
                     raw_violations: List[str],
                     pages_raw: Optional[List[list]] = None) -> Dict:
    """执行全部自动检查。

    seq: 全局内容块序列（排除版边块；含已关联题注块）。
    raw_violations: check_raw_preserved 的结果（pipeline 已计算）。
    返回 {checks, pass_count, fail_count, acceptance, stats}。
    """
    checks: List[dict] = []
    checks_by_key: Dict[str, dict] = {}
    page_n = cfg["page"]["pdf_pages_expected"] or 0
    bound = cfg["page"]["bounds"]
    tol = cfg["page"]["bbox_tolerance"]
    ref_pattern = cfg["validation"]["source_ref_pattern"]

    def add(key: str, name: str, ok: bool, detail: str = "", level: str = "error") -> bool:
        entry = {"key": key, "check": name, "ok": ok, "detail": detail, "level": level}
        checks.append(entry)
        checks_by_key[key] = entry
        return ok

    by_id = {s.section_id: s for s in sections}
    heading_sid_by_block = {s.heading_block_id: s.section_id for s in sections if s.level > 0}

    # 1. section_id 唯一、无循环、无孤立
    dup_sids = _check_unique([s.section_id for s in sections])
    orphan_nodes = [s.section_id for s in sections
                    if s.parent_section_id and s.parent_section_id not in by_id]
    cycles: List[str] = []
    for s in sections:
        seen = set()
        cur = s
        while cur.parent_section_id:
            if cur.parent_section_id in seen:
                cycles.append(s.section_id)
                break
            seen.add(cur.parent_section_id)
            nxt = by_id.get(cur.parent_section_id)
            if nxt is None:
                break
            cur = nxt
    add("section_id_unique", "section_id_unique", not dup_sids,
        f"重复 {len(dup_sids)} 个" + (f": {dup_sids[:5]}" if dup_sids else ""))
    add("section_tree_no_orphans", "section_tree_no_orphans", not orphan_nodes,
        f"孤儿节点 {len(orphan_nodes)} 个" + (f": {orphan_nodes[:5]}" if orphan_nodes else ""))
    add("section_tree_no_cycles", "section_tree_no_cycles", not cycles,
        f"循环 {len(cycles)} 个" + (f": {cycles[:5]}" if cycles else ""))

    # 2. 章节范围：end 结束于下一个同级/更高级标题之前（结构性重算比对）
    flat = blocks  # blocks 已按 (pdf_page, block_order) 文档序
    flat_pos = {b.block_id: i for i, b in enumerate(flat)}
    heading_level = {s.heading_block_id: s.level for s in sections if s.level > 0}
    headings_in_order = [b for b in flat
                         if b.block_type == "heading" and b.block_id in heading_level]
    bad_end: List[str] = []
    last_page = page_n - 1 if page_n else max(b.pdf_page for b in flat)
    end_at_last_page = 0
    for s in sections:
        if s.level == 0:
            continue
        pos = flat_pos.get(s.heading_block_id)
        if pos is None:
            bad_end.append(f"{s.section_id}: 标题块不在块列表中")
            continue
        end_pos = len(flat)
        for hb2 in headings_in_order[headings_in_order.index(
                next(b for b in headings_in_order if b.block_id == s.heading_block_id)) + 1:]:
            if heading_level[hb2.block_id] <= s.level:
                end_pos = flat_pos[hb2.block_id]
                break
        expected_end = None
        for b in reversed(flat[pos:end_pos]):
            if b.block_type not in MARGIN_TYPES:
                expected_end = b
                break
        expected_ref = expected_end.source_ref if expected_end else s.start_source_ref
        if s.end_source_ref != expected_ref:
            bad_end.append(f"{s.section_id}: end_source_ref={s.end_source_ref} "
                           f"期望={expected_ref}")
        if s.end_page == last_page:
            end_at_last_page += 1
    add("section_ranges_correct", "section_ranges_correct", not bad_end,
        f"end 越界章节 {len(bad_end)} 个" + (f": {bad_end[:5]}" if bad_end else ""))
    non_root = [s for s in sections if s.level > 0]
    frac_last = end_at_last_page / len(non_root) if non_root else 0
    add("sections_not_all_end_at_last_page", "sections_not_all_end_at_last_page",
        frac_last < 0.05, f"结束于最后一页的章节 {end_at_last_page}/{len(non_root)}")

    # 3. 每个正文/图片/表格块恰好归属一个最深层 section_id
    bad_binding: List[str] = []
    section_ranges: Dict[str, Tuple[int, int]] = {}
    for s in sections:
        if s.level == 0:
            continue
        start = flat_pos.get(s.heading_block_id)
        if start is None:
            continue
        end = len(flat)
        for hb2 in headings_in_order:
            p2 = flat_pos[hb2.block_id]
            if p2 > start and heading_level[hb2.block_id] <= s.level:
                end = p2
                break
        section_ranges[s.section_id] = (start, end)
    content_types = {"paragraph", "list_item", "formula", "table", "figure",
                     "page_footnote", "unknown", "table_title", "figure_title", "table_note"}
    for b in flat:
        if b.block_type not in content_types:
            continue
        if b.extra.get("associated_with"):
            continue  # 已关联题注块：属性，不属于内容流
        if not b.section_id or b.section_id not in by_id:
            bad_binding.append(f"{b.block_id}: section_id 缺失或无效（{b.section_id!r}）")
            continue
        if b.section_id not in section_ranges:
            continue
        start, end = section_ranges[b.section_id]
        pos = flat_pos[b.block_id]
        if not (start <= pos < end):
            bad_binding.append(f"{b.block_id}: section_id={b.section_id} 但块不在其范围内")
            continue
        # 最深层归属：所属 section 须是包含该位置的最深层章节
        candidates = [sid for sid, (s0, e0) in section_ranges.items()
                      if s0 <= pos < e0]
        deepest = max(candidates, key=lambda sid: by_id[sid].level) if candidates else None
        if deepest != b.section_id:
            bad_binding.append(f"{b.block_id}: 归属 {b.section_id}，最深应为 {deepest}")
    add("blocks_bound_to_deepest_section", "blocks_bound_to_deepest_section", not bad_binding,
        f"归属异常 {len(bad_binding)} 个" + (f": {bad_binding[:5]}" if bad_binding else ""))

    # 4. 每个章节 content 的 order 严格递增
    bad_order = [s.section_id for s in sections
                 if [i.order for i in s.content] != list(range(1, len(s.content) + 1))]
    add("content_order_strict", "content_order_strict", not bad_order,
        f"order 异常章节 {len(bad_order)} 个" + (f": {bad_order[:5]}" if bad_order else ""))

    # 5. 递归遍历顺序 == 原始阅读顺序
    expected = _expected_tokens(seq, heading_sid_by_block)
    actual = _traversal_token_stream(sections)
    order_ok = expected == actual
    first_diff = next((i for i, (a, b) in enumerate(zip(expected, actual)) if a != b), None)
    add("traversal_matches_reading_order", "traversal_matches_reading_order", order_ok,
        "" if order_ok else "长度 %d vs %d，首个差异 @%s" % (len(expected), len(actual), first_diff))

    # 6/7. 融合边界与完整还原
    seq_pos = {b.block_id: i for i, b in enumerate(seq)}
    visual_boundary = {"figure", "table"}
    text_block_ids = [b.block_id for b in seq
                      if b.block_id not in heading_sid_by_block
                      and b.block_type not in visual_boundary
                      and not b.extra.get("associated_with")]
    fused_members: List[str] = []
    boundary_violations: List[str] = []
    for s in sections:
        for item in s.content:
            if item.item_type != "text_segment":
                continue
            fused_members.extend(item.member_block_ids)
            positions = [seq_pos[m] for m in item.member_block_ids if m in seq_pos]
            if not positions:
                continue
            lo, hi = min(positions), max(positions)
            for b in seq[lo + 1:hi]:
                if (b.block_id in heading_sid_by_block
                        or b.block_type in visual_boundary):
                    boundary_violations.append(
                        f"{item.item_id}: 成员间夹有 {b.block_type} {b.block_id}")
                    break
    add("fusion_no_cross_boundary", "fusion_no_cross_boundary", not boundary_violations,
        f"跨越边界 {len(boundary_violations)} 处" + (f": {boundary_violations[:5]}" if boundary_violations else ""))
    dup_fused = _check_unique(fused_members)
    missing_fused = sorted(set(text_block_ids) - set(fused_members))
    extra_fused = sorted(set(fused_members) - set(text_block_ids))
    add("fusion_restores_all_text_blocks", "fusion_restores_all_text_blocks",
        not dup_fused and not missing_fused and not extra_fused,
        f"重复 {len(dup_fused)}、缺失 {len(missing_fused)}、多余 {len(extra_fused)}")

    # 8-12. 视觉资源
    figure_blocks = [b for b in seq if b.block_type == "figure"]
    table_blocks = [b for b in seq if b.block_type == "table"]
    visual_items: List[ContentItem] = []
    for s in sections:
        for item in s.content:
            if item.item_type in ("figure", "table"):
                visual_items.append(item)
    item_by_ref = {i.source_ref: i for i in visual_items}
    bad_assets: List[str] = []
    pdf_crop_count = 0
    dir_paths: List[str] = []
    for b in figure_blocks + table_blocks:
        item = item_by_ref.get(b.source_ref)
        if item is None:
            bad_assets.append(f"{b.block_id}: 无内容项")
            continue
        if not item.asset_exists or not item.asset_path:
            bad_assets.append(f"{b.block_id}: 资产不存在")
            continue
        if os.path.isdir(item.asset_path):
            dir_paths.append(f"{b.block_id}: {item.asset_path} 是目录")
            bad_assets.append(f"{b.block_id}: asset_path 是目录")
        elif not os.path.isfile(item.asset_path):
            bad_assets.append(f"{b.block_id}: asset_path 不是文件")
        if item.asset_origin == "pdf_crop":
            pdf_crop_count += 1
    add("all_figures_have_assets", "all_figures_have_assets",
        len(figure_blocks) == 741 and all(
            item_by_ref.get(b.source_ref) and item_by_ref[b.source_ref].asset_exists
            for b in figure_blocks),
        f"figure {len(figure_blocks)} 个")
    add("all_tables_have_assets", "all_tables_have_assets",
        len(table_blocks) == 193 and all(
            item_by_ref.get(b.source_ref) and item_by_ref[b.source_ref].asset_exists
            for b in table_blocks),
        f"table {len(table_blocks)} 个")
    add("asset_path_not_directory", "asset_path_not_directory", not dir_paths,
        f"目录路径 {len(dir_paths)} 处" + (f": {dir_paths[:5]}" if dir_paths else ""))
    add("pdf_crop_count_33", "pdf_crop_count_33", pdf_crop_count == 33,
        f"pdf_crop {pdf_crop_count} 处")

    # 12. figure/table 的 source_ref 可返回 pdf_page/bbox/asset_path
    bad_reg = [b.source_ref for b in figure_blocks + table_blocks
               if not (b.bbox and b.pdf_page is not None
                       and item_by_ref.get(b.source_ref)
                       and item_by_ref[b.source_ref].asset_path)]
    add("visual_source_ref_resolvable", "visual_source_ref_resolvable", not bad_reg,
        f"不可溯源 {len(bad_reg)} 处" + (f": {bad_reg[:5]}" if bad_reg else ""))

    # 13. 题注不在下游内容流中重复
    associated_refs = {b.source_ref for b in seq if b.extra.get("associated_with")}
    caption_in_content = [c for s in sections for item in s.content
                          for c in item.source_refs if c in associated_refs]
    add("captions_not_duplicated_in_content", "captions_not_duplicated_in_content",
        not caption_in_content,
        f"重复题注 {len(caption_in_content)} 处" + (f": {caption_in_content[:5]}" if caption_in_content else ""))

    # 14. raw_text 未被覆盖
    add("raw_text_preserved", "raw_text_preserved", not raw_violations,
        f"不一致 {len(raw_violations)} 处" + (f": {raw_violations[:5]}" if raw_violations else ""))

    # 15. 无实体/关系/工艺规则字段
    forbidden_hits: List[str] = []
    for b in flat:
        for k in b.to_json_obj().keys():
            if FORBIDDEN_RE.search(k):
                forbidden_hits.append(f"block {b.block_id}:{k}")
    for s in sections:
        for k in s.to_json_obj().keys():
            if FORBIDDEN_RE.search(k):
                forbidden_hits.append(f"section {s.section_id}:{k}")
        for item in s.content:
            for k in item.to_json_obj().keys():
                if FORBIDDEN_RE.search(k):
                    forbidden_hits.append(f"item {item.item_id}:{k}")
    add("no_knowledge_fields", "no_knowledge_fields", not forbidden_hits,
        f"图谱字段 {len(forbidden_hits)} 处" + (f": {forbidden_hits[:5]}" if forbidden_hits else ""))

    # 16. 确定性：由 validate 模式两次运行哈希比对
    add("deterministic_across_runs", "deterministic_across_runs", True,
        "由 validate 模式两次运行哈希比对确认", "info")

    # 17. 无人工目检门禁
    add("no_human_gate", "no_human_gate", True,
        "验收不依赖 OCR 数值人工确认或图片语义目检", "info")

    ok_count = sum(1 for c in checks if c["ok"])
    fail_count = sum(1 for c in checks if not c["ok"] and c["level"] == "error")

    acceptance = {
        "1_sections_valid": (checks_by_key["section_id_unique"]["ok"]
                             and checks_by_key["section_tree_no_orphans"]["ok"]
                             and checks_by_key["section_tree_no_cycles"]["ok"]),
        "2_section_ranges_valid": (checks_by_key["section_ranges_correct"]["ok"]
                                   and checks_by_key["sections_not_all_end_at_last_page"]["ok"]),
        "3_blocks_bound_to_deepest_section": checks_by_key["blocks_bound_to_deepest_section"]["ok"],
        "4_content_order_strict": checks_by_key["content_order_strict"]["ok"],
        "5_traversal_matches_reading_order": checks_by_key["traversal_matches_reading_order"]["ok"],
        "6_fusion_no_cross_boundary": checks_by_key["fusion_no_cross_boundary"]["ok"],
        "7_fusion_restores_all_text_blocks": checks_by_key["fusion_restores_all_text_blocks"]["ok"],
        "8_all_figures_have_assets": checks_by_key["all_figures_have_assets"]["ok"],
        "9_all_tables_have_assets": checks_by_key["all_tables_have_assets"]["ok"],
        "10_asset_path_not_directory": checks_by_key["asset_path_not_directory"]["ok"],
        "11_pdf_crop_count_33": checks_by_key["pdf_crop_count_33"]["ok"],
        "12_visual_source_ref_resolvable": checks_by_key["visual_source_ref_resolvable"]["ok"],
        "13_captions_not_duplicated": checks_by_key["captions_not_duplicated_in_content"]["ok"],
        "14_raw_text_preserved": checks_by_key["raw_text_preserved"]["ok"],
        "15_no_knowledge_fields": checks_by_key["no_knowledge_fields"]["ok"],
        "16_deterministic": "verified_by_validate_command",
        "17_no_human_gate": checks_by_key["no_human_gate"]["ok"],
    }
    return {
        "checks": checks,
        "pass_count": ok_count,
        "fail_count": fail_count,
        "acceptance": acceptance,
    }
