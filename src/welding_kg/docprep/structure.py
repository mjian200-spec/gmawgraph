"""标题—有序内容结构构建 + 文本融合（任务书 v2 第 4.1、5.5 节）。

- 每个章节 content 为直接属于该标题的有序内容流；
- 连续文本块融合为 text_segment（边界：标题/图片/表格），跨页可融合但
  中间不得有标题、图片或表格；
- 子标题处插入 subsection 引用，子章节内容只保存在子章节中；
- 递归遍历 section.content 可恢复完整文档阅读顺序；
- 融合禁止为了“通顺”生成/改写/补充内容；member_block_ids 可完整还原。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .model import Block, ContentItem, SectionNode

MARGIN_TYPES = {"page_header", "page_footer", "page_number", "page_aside_text"}


def build_content_sequence(pages: List[List[Block]], cfg: dict) -> Tuple[List[Block], dict]:
    """构建全局内容块序列（排除版边块；已关联题注块保留但标记）。

    返回 (序列, 统计)。关联题注块在序列中标记 skip（由章节内容构建时跳过）。
    """
    excluded = set(cfg["exclude_from_extraction"])
    seq: List[Block] = []
    stats = {"total": 0, "skipped_margin": 0}
    for page in pages:
        for b in page:
            if b.block_type in excluded:
                stats["skipped_margin"] += 1
                continue
            seq.append(b)
            stats["total"] += 1
    return seq, stats


def _item_factory(item_type: str, seq_counter: List[int]) -> ContentItem:
    """按文档顺序分配稳定 item_id（gmaw:v2:item:{abbr}{序号}）。"""
    abbr = {"text_segment": "seg", "figure": "fig", "table": "tab"}[item_type]
    seq_counter[0] += 1
    return ContentItem(item_type=item_type,
                       item_id=f"gmaw:v2:item:{abbr}{seq_counter[0]:04d}",
                       order=0)


def build_section_content(sections: List[SectionNode], seq: List[Block],
                          cfg: dict, assets_by_ref: Dict[str, dict]) -> dict:
    """为每个章节构建 content（有序项流），并返回统计与问题。

    assets_by_ref: source_ref → 资产记录（由阶段资产解析提供）。
    """
    text_types = set(cfg["fusion"]["text_types"])
    node_by_id = {s.section_id: s for s in sections}
    heading_node_by_block: Dict[str, SectionNode] = {}
    for s in sections:
        if s.heading_block_id:
            heading_node_by_block[s.heading_block_id] = s

    # 每个标题块在内容序列中的位置（block_id → seq 下标）
    seq_pos = {b.block_id: i for i, b in enumerate(seq)}

    stats = {"text_segments": 0, "figures": 0, "tables": 0, "subsections": 0,
             "skipped_captions": 0, "unassociated_captions": 0}
    issues: List[dict] = []
    seq_counter = [0]

    def make_text_segment(members: List[Block], section: SectionNode) -> ContentItem:
        stats["text_segments"] += 1
        item = _item_factory("text_segment", seq_counter)
        item.order = 0  # 稍后统一编号
        item.section_id = section.section_id
        item.heading_path = list(members[0].heading_path)
        item.text = "".join(m.normalized_text for m in members)
        item.member_block_ids = [m.block_id for m in members]
        item.source_refs = [m.source_ref for m in members]
        item.sources = [{"pdf_page": m.pdf_page, "bbox": m.bbox,
                         "source_ref": m.source_ref} for m in members]
        return item

    def make_visual_item(b: Block, section: SectionNode, asset: dict) -> ContentItem:
        if b.block_type == "figure":
            stats["figures"] += 1
        else:
            stats["tables"] += 1
        item = _item_factory(b.block_type, seq_counter)
        item.section_id = section.section_id
        item.heading_path = list(b.heading_path)
        item.pdf_page = b.pdf_page
        item.bbox = list(b.bbox)
        item.source_ref = b.source_ref
        item.asset_path = asset.get("asset_path", "")
        item.original_asset_path = asset.get("original_asset_path") or ""
        item.asset_origin = asset.get("asset_origin", "")
        item.asset_exists = bool(asset.get("asset_exists"))
        item.sha256 = asset.get("sha256", "")
        item.mime_type = asset.get("mime_type", "")
        item.caption = b.extra.get("caption_text", "")
        item.footnotes = list(b.extra.get("footnotes", []))
        if b.block_type == "table":
            item.ocr_html = b.extra.get("ocr_html", "")
        if not item.caption:
            item.issues.append({"code": "caption_missing",
                                "message": "未找到图题/表题",
                                "severity": "warning"})
        return item

    def make_subsection_item(child: SectionNode) -> ContentItem:
        stats["subsections"] += 1
        return ContentItem(item_type="subsection", item_id=child.section_id, order=0)

    # 每个章节在内容序列中的范围
    # [start_pos, end_pos)：start = 标题块位置；end = 下一个 level<=level 的标题位置
    section_ranges: Dict[str, Tuple[int, int]] = {}
    heading_level_by_block: Dict[str, int] = {}
    for s in sections:
        if s.level == 0:
            continue
        heading_level_by_block[s.heading_block_id] = s.level
    seq_headings = [(i, b) for i, b in enumerate(seq)
                    if b.block_type == "heading" and b.block_id in heading_node_by_block]
    for idx, (pos, hb) in enumerate(seq_headings):
        node = heading_node_by_block[hb.block_id]
        end = len(seq)
        for pos2, hb2 in seq_headings[idx + 1:]:
            if heading_level_by_block[hb2.block_id] <= node.level:
                end = pos2
                break
        section_ranges[node.section_id] = (pos, end)

    # 构建每个章节的 content（含根节点：首个标题前的内容 + 顶层章节引用）
    def walk(s: SectionNode, start: int, end: int) -> List[ContentItem]:
        pending_text: List[Block] = []
        items: List[ContentItem] = []

        def flush() -> None:
            if pending_text:
                items.append(make_text_segment(pending_text, s))
                pending_text.clear()

        i = start
        while i < end:
            b = seq[i]
            # 已关联题注/脚注块：作为视觉项属性，不在内容流中重复
            if b.extra.get("associated_with"):
                stats["skipped_captions"] += 1
                i += 1
                continue
            if b.block_type == "heading":
                if b.block_id in heading_node_by_block:
                    child = heading_node_by_block[b.block_id]
                    if child.section_id == s.section_id:
                        # 本 section 自己的标题：内容流起点，跳过
                        i += 1
                        continue
                    if child.level > s.level:
                        flush()
                        items.append(make_subsection_item(child))
                        # 跳过整个子章节范围
                        i = section_ranges[child.section_id][1]
                        continue
                    # 同级/更高级标题：本 section 的结束边界
                    break
                # 无法归入树（跨行合并/待复核标题）：按文本保留，不得丢失
                pending_text.append(b)
                i += 1
                continue
            if b.block_type in ("figure", "table"):
                flush()
                asset = assets_by_ref.get(b.source_ref, {})
                items.append(make_visual_item(b, s, asset))
                i += 1
                continue
            if b.block_type in text_types:
                pending_text.append(b)
                i += 1
                continue
            # 其余类型（unassociated 题注等）按文本处理并标记
            pending_text.append(b)
            stats["unassociated_captions"] += 1
            issues.append({"code": "unassociated_caption_or_note",
                           "block_id": b.block_id,
                           "message": f"{b.block_type} 未关联到视觉项，按文本保留于内容流"})
            i += 1
        flush()
        return items

    for s in sections:
        if s.level == 0:
            continue
        start, end = section_ranges[s.section_id]
        items = walk(s, start, end)
        for order, item in enumerate(items, start=1):
            item.order = order
        s.content = items

    # 根节点：首个标题前的内容 + 文档序的顶层章节引用
    root = node_by_id["gmaw:v2:sec:root"]
    first_heading_pos = seq_headings[0][0] if seq_headings else len(seq)
    root_items = walk(root, 0, first_heading_pos)
    for child_id in root.children:
        root_items.append(make_subsection_item(node_by_id[child_id]))
    for order, item in enumerate(root_items, start=1):
        item.order = order
    root.content = root_items

    return stats, issues
