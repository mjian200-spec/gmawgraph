"""阶段 B：内容块标准化。

把原始解析块转换为标准 Block：类型统一、raw_text 提取、
图题/表题拆分（保留物理来源）、列表拆项。
表格 HTML 仅作为 ocr_html 辅助字段原样保留（任务书 v2 第 7 节：
不是原图的替代品，本阶段不判断其内容是否正确）。

derive_raw_blocks 是原始块 → (标准类型, raw_text) 序列的纯函数，
normalize_pages 与 raw_text 核验（验收 14）共用同一推导逻辑。
"""

from __future__ import annotations

from typing import List, Tuple

from .model import Block
from .textutil import concat_content


def _plain_text(pieces: list) -> str:
    return concat_content(pieces)


def derive_raw_blocks(raw: dict, p_idx: int, b_idx: int) -> List[Tuple[str, str]]:
    """原始块 → [(标准类型, raw_text)] 序列（纯推导，与 normalize_pages 共用）。"""
    orig_type = raw.get("type") or "unknown"
    content = raw.get("content") or {}

    if orig_type == "title":
        return [("heading", _plain_text(content.get("title_content") or []))]
    if orig_type == "paragraph":
        return [("paragraph", _plain_text(content.get("paragraph_content") or []))]
    if orig_type == "equation_interline":
        return [("formula", content.get("math_content") or "")]
    if orig_type == "list":
        items = content.get("list_items") or []
        if not items:
            return [("list_item", "")]
        return [("list_item", _plain_text(item.get("item_content") or []))
                for item in items]
    if orig_type == "table":
        out = [("table", "")]
        caption = content.get("table_caption") or []
        if caption:
            out.append(("table_title", _plain_text(caption)))
        footnote = content.get("table_footnote") or []
        if footnote:
            out.append(("table_note", _plain_text(footnote)))
        return out
    if orig_type == "image":
        out = [("figure", "")]
        caption = content.get("image_caption") or []
        if caption:
            out.append(("figure_title", _plain_text(caption)))
        footnote = content.get("image_footnote") or []
        if footnote:
            out.append(("table_note", _plain_text(footnote)))
        return out
    if orig_type == "page_header":
        return [("page_header", _plain_text(content.get("page_header_content") or []))]
    if orig_type == "page_footer":
        return [("page_footer", _plain_text(content.get("page_footer_content") or []))]
    if orig_type == "page_number":
        return [("page_number", _plain_text(content.get("page_number_content") or []))]
    if orig_type == "page_footnote":
        return [("page_footnote", _plain_text(content.get("page_footnote_content") or []))]
    if orig_type == "page_aside_text":
        return [("page_aside_text", _plain_text(content.get("page_aside_text_content") or []))]
    return [("unknown", "")]


def normalize_pages(pages: List[list], cfg: dict) -> List[List[Block]]:
    """原始页列表 → 标准 Block 页列表。raw_text 只提取不改写。"""
    type_map = cfg["block_types"]
    out_pages: List[List[Block]] = []

    for p_idx, page in enumerate(pages):
        out: List[Block] = []
        for b_idx, raw in enumerate(page):
            orig_type = raw.get("type") or "unknown"
            std_type = type_map.get(orig_type, "unknown")
            bbox = raw.get("bbox") if isinstance(raw.get("bbox"), list) else None
            content = raw.get("content") or {}
            extra: dict = {"parent_origin": [p_idx, b_idx]}

            def make_block(btype: str, text: str, ex: dict = None, issues: list = None) -> Block:
                b = Block(
                    block_id="",  # 阶段 C 分配
                    block_type=btype,
                    pdf_page=p_idx,
                    printed_page=None,
                    printed_page_numeric=None,
                    block_order=-1,  # 阶段 C 分配
                    bbox=list(bbox) if bbox else [],
                    raw_text=text,
                    normalized_text="",
                    heading_path=[],
                    source_ref="",  # 阶段 C 分配
                    status="normal",
                    issues=issues or [],
                    original_type=orig_type,
                    original_index=b_idx,
                    extra=ex or extra,
                )
                return b

            if orig_type == "title":
                text = _plain_text(content.get("title_content") or [])
                ex = dict(extra)
                ex["original_level"] = content.get("level")
                out.append(make_block("heading", text, ex))

            elif orig_type == "paragraph":
                text = _plain_text(content.get("paragraph_content") or [])
                out.append(make_block("paragraph", text))

            elif orig_type == "equation_interline":
                latex = content.get("math_content") or ""
                ex = dict(extra)
                ex["latex"] = latex
                ex["math_type"] = content.get("math_type")
                ex["image_path"] = (content.get("image_source") or {}).get("path") or None
                out.append(make_block("formula", latex, ex))

            elif orig_type == "list":
                items = content.get("list_items") or []
                ex0 = dict(extra)
                ex0["list_type"] = content.get("list_type")
                if not items:
                    out.append(make_block("list_item", "", ex0,
                                          [{"code": "empty_list", "message": "列表块无条目",
                                            "severity": "warning"}]))
                else:
                    for i_idx, item in enumerate(items):
                        text = _plain_text(item.get("item_content") or [])
                        ex = dict(ex0)
                        ex["list_item_index"] = i_idx
                        issues = [{
                            "code": "list_bbox_shared",
                            "message": "bbox 为整列表块包围盒，非单条包围盒",
                            "severity": "info",
                        }]
                        out.append(make_block("list_item", text, ex, issues))

            elif orig_type == "table":
                caption = content.get("table_caption") or []
                footnote = content.get("table_footnote") or []
                html = content.get("html") or ""
                ex = dict(extra)
                ex["ocr_html"] = html                # 辅助字段，非原图替代品
                ex["image_path"] = (content.get("image_source") or {}).get("path") or None
                ex["table_type"] = content.get("table_type")
                ex["table_nest_level"] = content.get("table_nest_level")
                out.append(make_block("table", "", ex))
                if caption:
                    ex_c = dict(extra)
                    ex_c["parent_origin"] = [p_idx, b_idx]
                    issues_c = [{"code": "caption_bbox_inherited",
                                 "message": "图题/表题 bbox 继承自母块包围盒（解析器未给独立坐标）",
                                 "severity": "info"}]
                    out.append(make_block("table_title", _plain_text(caption), ex_c, issues_c))
                if footnote:
                    out.append(make_block("table_note", _plain_text(footnote), dict(extra)))

            elif orig_type == "image":
                caption = content.get("image_caption") or []
                footnote = content.get("image_footnote") or []
                ex = dict(extra)
                ex["image_path"] = (content.get("image_source") or {}).get("path") or None
                out.append(make_block("figure", "", ex))
                if caption:
                    ex_c = dict(extra)
                    ex_c["parent_origin"] = [p_idx, b_idx]
                    issues_c = [{"code": "caption_bbox_inherited",
                                 "message": "图题/表题 bbox 继承自母块包围盒（解析器未给独立坐标）",
                                 "severity": "info"}]
                    out.append(make_block("figure_title", _plain_text(caption), ex_c, issues_c))
                if footnote:
                    out.append(make_block("table_note", _plain_text(footnote), dict(extra)))

            elif orig_type == "page_header":
                out.append(make_block("page_header", _plain_text(content.get("page_header_content") or [])))

            elif orig_type == "page_footer":
                out.append(make_block("page_footer", _plain_text(content.get("page_footer_content") or [])))

            elif orig_type == "page_number":
                out.append(make_block("page_number", _plain_text(content.get("page_number_content") or [])))

            elif orig_type == "page_footnote":
                out.append(make_block("page_footnote", _plain_text(content.get("page_footnote_content") or [])))

            elif orig_type == "page_aside_text":
                out.append(make_block("page_aside_text", _plain_text(content.get("page_aside_text_content") or [])))

            else:
                issues = [{"code": "unknown_type",
                           "message": f"无法可靠归类，原类型: {orig_type}",
                           "severity": "warning"}]
                out.append(make_block("unknown", "", dict(extra), issues))

        out_pages.append(out)

    return out_pages
