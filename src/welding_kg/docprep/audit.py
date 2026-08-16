"""阶段 A：内容块审计（只读，任务书 v2 第 5.1 节）。

统计输入结构与资源可用性分类（file/dir/missing），结果写入
preprocessing_report.json 的 input_audit 部分。审计模式不生成任何
补裁资源（pdf_crop 只在 run 模式生成）。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import List, Optional, Tuple

from .assets import classify_image_path
from .textutil import concat_content

# 罗马数字（含 Unicode 罗马数字字符）
_ROMAN_UNICODE = {
    "Ⅰ": 1, "Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "Ⅴ": 5, "Ⅵ": 6, "Ⅶ": 7, "Ⅷ": 8, "Ⅸ": 9,
    "Ⅹ": 10, "Ⅺ": 11, "Ⅻ": 12,
}
_ROMAN_ASCII_RE = re.compile(r"^[IVXLCDMivxlcdm]+$")


def roman_to_int(text: str) -> Optional[int]:
    """罗马数字 → 整数（ASCII 或 Unicode 形式）。"""
    t = text.strip()
    if not t:
        return None
    if t in _ROMAN_UNICODE:
        return _ROMAN_UNICODE[t]
    if _ROMAN_ASCII_RE.match(t):
        value = 0
        vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        prev = 0
        for ch in reversed(t.upper()):
            cur = vals[ch]
            value += -cur if cur < prev else cur
            prev = cur
        return value
    return None


def normalize_page_number_text(text: str) -> str:
    """页码块文本规范化：去空白、全角转半角。"""
    t = text.strip().replace("　", " ")
    t = re.sub(r"\s+", "", t)
    full = "０１２３４５６７８９"
    t = t.translate(str.maketrans(full, "0123456789"))
    return t


def classify_printed_page(text: str) -> Tuple[Optional[str], Optional[str]]:
    """印刷页码分类。返回 (类别 arabic/roman, 规范化页码字符串)。"""
    t = normalize_page_number_text(text)
    if not t:
        return None, ""
    if re.fullmatch(r"\d+", t):
        return "arabic", str(int(t))
    if roman_to_int(t) is not None:
        return "roman", t
    return None, t


def _raw_block_text(block: dict) -> str:
    """提取任意原始块的可读文本（用于审计）。"""
    btype = block.get("type", "")
    content = block.get("content", {})
    if btype == "title":
        return concat_content(content.get("title_content"))
    if btype == "paragraph":
        return concat_content(content.get("paragraph_content"))
    if btype == "page_header":
        return concat_content(content.get("page_header_content"))
    if btype == "page_footer":
        return concat_content(content.get("page_footer_content"))
    if btype == "page_number":
        return concat_content(content.get("page_number_content"))
    if btype == "page_footnote":
        return concat_content(content.get("page_footnote_content"))
    if btype == "page_aside_text":
        return concat_content(content.get("page_aside_text_content"))
    if btype == "equation_interline":
        return content.get("math_content", "")
    if btype == "list":
        return "".join(concat_content(i.get("item_content")) for i in content.get("list_items", []))
    if btype == "table":
        parts = [concat_content(content.get("table_caption")),
                 concat_content(content.get("table_footnote"))]
        return " ".join(p for p in parts if p)
    if btype == "image":
        parts = [concat_content(content.get("image_caption")),
                 concat_content(content.get("image_footnote"))]
        return " ".join(p for p in parts if p)
    return ""


def audit(pages: List[list], cfg: dict, pdf_path: Optional[str],
          pdf_pages: Optional[int], pdf_method: str,
          ocr_json_dir: str) -> dict:
    """执行只读审计，返回 input_audit 字典。"""
    n_pages = len(pages)
    bound = cfg["page"]["bounds"]
    tol = cfg["page"]["bbox_tolerance"]

    type_counts: Counter = Counter()
    title_levels: Counter = Counter()
    empty_text_blocks: List[dict] = []
    short_blocks: List[dict] = []
    duplicate_blocks: List[dict] = []
    missing_bbox: List[dict] = []
    missing_type: List[dict] = []
    oob_bbox: List[dict] = []
    captionless_images = 0
    captionless_tables = 0
    page_number_blocks: List[dict] = []
    printed_page_mapping: List[dict] = []
    table_html_stats = {"with_html": 0, "without_html": 0}
    pages_without_pagenum: List[int] = []
    page_block_counts: List[int] = []
    asset_class = {"figure": Counter(), "table": Counter()}

    for p_idx, page in enumerate(pages):
        page_block_counts.append(len(page))
        pagenum_texts = []
        for b_idx, block in enumerate(page):
            btype = block.get("type")
            if not btype:
                missing_type.append({"pdf_page": p_idx, "index": b_idx})
                btype = "unknown"
            type_counts[btype] += 1
            bbox = block.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4 or not all(
                isinstance(v, (int, float)) for v in bbox
            ):
                missing_bbox.append({"pdf_page": p_idx, "index": b_idx, "type": btype})
            elif (bbox[0] < bound[0] - tol or bbox[1] < bound[1] - tol
                    or bbox[2] > bound[2] + tol or bbox[3] > bound[3] + tol):
                oob_bbox.append({"pdf_page": p_idx, "index": b_idx, "bbox": bbox})

            text = _raw_block_text(block)
            content = block.get("content", {}) or {}

            if btype == "title":
                title_levels[content.get("level", -1)] += 1
            if btype == "page_number":
                cat, val = classify_printed_page(text)
                page_number_blocks.append({"pdf_page": p_idx, "text": text, "class": cat})
                if val:
                    pagenum_texts.append((cat, val))
            if btype in ("paragraph", "list"):
                plain = text.strip()
                if not plain:
                    empty_text_blocks.append({"pdf_page": p_idx, "index": b_idx, "type": btype})
                elif btype == "paragraph" and len(plain) < 4:
                    short_blocks.append({"pdf_page": p_idx, "index": b_idx, "text": plain})
            if btype == "image":
                if not content.get("image_caption"):
                    captionless_images += 1
                asset_class["figure"][classify_image_path(
                    (content.get("image_source") or {}).get("path"), ocr_json_dir)] += 1
            if btype == "table":
                if not content.get("table_caption"):
                    captionless_tables += 1
                html = content.get("html") or ""
                table_html_stats["with_html" if html.strip() else "without_html"] += 1
                asset_class["table"][classify_image_path(
                    (content.get("image_source") or {}).get("path"), ocr_json_dir)] += 1

        # 页内重复块
        seen = {}
        for b_idx, block in enumerate(page):
            key = (block.get("type"), _raw_block_text(block)[:80], tuple(block.get("bbox") or ()))
            if key in seen:
                duplicate_blocks.append({"pdf_page": p_idx, "index": b_idx,
                                         "dup_of_index": seen[key]})
            else:
                seen[key] = b_idx

        if pagenum_texts:
            kinds = [c for c, _ in pagenum_texts]
            values = [v for _, v in pagenum_texts]
            printed_page_mapping.append({
                "pdf_page": p_idx, "classes": kinds, "values": values,
            })
        else:
            pages_without_pagenum.append(p_idx)

    return {
        "pdf_pages": pdf_pages,
        "pdf_page_count_method": pdf_method,
        "parsed_pages": n_pages,
        "pdf_page_index_continuous": pdf_pages is None or pdf_pages == n_pages,
        "block_type_counts": dict(sorted(type_counts.items())),
        "blocks_per_page": {"min": min(page_block_counts), "max": max(page_block_counts),
                            "avg": round(sum(page_block_counts) / n_pages, 2),
                            "total": sum(page_block_counts)},
        "empty_text_blocks": {"count": len(empty_text_blocks), "samples": empty_text_blocks[:20]},
        "short_blocks": {"count": len(short_blocks), "samples": short_blocks[:20]},
        "duplicate_blocks": {"count": len(duplicate_blocks), "samples": duplicate_blocks[:20]},
        "missing_bbox": {"count": len(missing_bbox), "samples": missing_bbox[:10]},
        "missing_type": {"count": len(missing_type), "samples": missing_type[:10]},
        "bbox_out_of_bounds": {"count": len(oob_bbox), "samples": oob_bbox[:10]},
        "title_level_distribution": dict(sorted(title_levels.items())),
        "title_level_all_same": len(set(title_levels)) <= 1,
        "captionless_images": captionless_images,
        "captionless_tables": captionless_tables,
        "table_html": table_html_stats,
        "asset_path_classification": {
            "figure": dict(sorted(asset_class["figure"].items())),
            "table": dict(sorted(asset_class["table"].items())),
        },
        "printed_page": {
            "page_number_blocks": len(page_number_blocks),
            "pages_without_pagenum": pages_without_pagenum,
            "mapping_samples": printed_page_mapping[:15],
            "mapping_samples_tail": printed_page_mapping[-5:],
        },
    }
