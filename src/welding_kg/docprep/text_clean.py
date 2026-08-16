"""阶段 E：正文清洗（格式级，任务书 v2 第 5.2 节）。

只做格式级清洗写 normalized_text；raw_text 不动。文本融合由
structure 阶段完成（本模块不再做跨页段落合并）。
"""

from __future__ import annotations

from typing import Dict, List

from .model import Block
from .textutil import clean_text

TEXT_TYPES = {"heading", "paragraph", "list_item", "table_title", "table_note",
              "figure_title", "page_header", "page_footer", "page_number",
              "page_footnote", "page_aside_text"}


def clean_pages(pages: List[List[Block]], cfg: dict) -> dict:
    """对全部块执行清洗；返回应用统计。"""
    rules = cfg["text_clean"]
    header_strip = rules.get("strip_header_prefix", True)
    counts: Dict[str, int] = {}

    for p_idx, page in enumerate(pages):
        header_texts = set()
        for b in page:
            if b.block_type in ("page_header", "page_footer"):
                t = b.normalized_text.strip()
                if t:
                    header_texts.add(t)
        for b in page:
            kind = b.block_type if b.block_type in TEXT_TYPES else "other"
            # 标题/正文拆分块：清洗来源优先取拆分后的文本
            src_text = b.raw_text
            if b.extra.get("split_title") or b.extra.get("split_body"):
                src_text = b.extra.get("split_title") or b.extra.get("split_body") or ""
            text, applied = clean_text(src_text, kind, rules)
            b.normalized_text = text
            # 题注：去除首尾混入的印刷页码残片
            if b.block_type in ("table_title", "figure_title"):
                from .textutil import clean_caption
                b.normalized_text = clean_caption(b.normalized_text)
            for rule in applied:
                counts[rule] = counts.get(rule, 0) + 1
            # 正文段落首部的页眉残留：如段落以页眉文本开头，则剥离（记入 normalized 即可）
            if (b.block_type == "paragraph" and header_strip and header_texts):
                for h in sorted(header_texts, key=len, reverse=True):
                    if text.startswith(h) and len(h) >= 6:
                        b.normalized_text = text[len(h):].lstrip(" 。，、")
                        b.add_issue("header_residue_stripped",
                                    f"段落首部剥离页眉残留文本: {h!r}", "info")
                        counts["header_residue_stripped"] = counts.get("header_residue_stripped", 0) + 1
                        break
            # 状态更新：清洗后文本为空时标记
            if b.block_type in ("paragraph", "list_item") and not b.normalized_text.strip():
                b.status = "needs_review"
                b.add_issue("empty_text", "清洗后文本为空", "warning")
    return counts
