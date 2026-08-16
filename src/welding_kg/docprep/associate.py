"""阶段 G：图片/表格与题注、脚注的版面结构关联（任务书 v2 第 5.6 节）。

题注作为视觉项的属性；原始题注块保留在内部块输出中供审计，
但不得在下游有序内容中重复出现（由 structure 阶段跳过已关联题注块）。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .model import Block
from .textutil import clean_caption, normalize_caption_no

_CAPTION_RE = re.compile(r"^[图表]\s*\d+[-－—–~]?\d*")


def _vertical_gap(a: Block, b: Block) -> Optional[float]:
    """两块的垂直间隙（无 bbox 时为 None）。"""
    if not a.bbox or not b.bbox:
        return None
    if a.bbox[3] <= b.bbox[1]:
        return b.bbox[1] - a.bbox[3]
    if b.bbox[3] <= a.bbox[1]:
        return a.bbox[1] - b.bbox[3]
    return 0.0  # 重叠


def associate(pages: List[List[Block]], cfg: dict) -> dict:
    """执行关联：内嵌题注/脚注 + 邻域分离题注。

    关联成功的题注块：extra["associated_with"] = 母块 block_id；
    母块：extra["caption_block_id"] / ["caption_text"] / ["caption_no"] /
    ["footnote_block_ids"]。
    """
    assoc_cfg = cfg["association"]
    caption_gap = assoc_cfg["caption_gap"]
    min_conf = assoc_cfg["min_confidence"]

    blocks = [b for page in pages for b in page]
    embedded = 0
    separated = 0
    unassociated: List[dict] = []
    associations: List[dict] = []

    # 1) 内嵌题注/脚注关联：normalize 阶段按 parent_origin 拆分出的块
    for b in blocks:
        if b.block_type in ("figure_title", "table_title", "table_note"):
            origin = b.extra.get("parent_origin")
            if not origin:
                continue
            parent = None
            for cand in pages[origin[0]]:
                if (cand.original_index == origin[1]
                        and cand.block_type in ("table", "figure")):
                    parent = cand
                    break
            if parent is None:
                b.add_issue("caption_parent_missing",
                            f"未找到题注母块 (page {origin[0]}, index {origin[1]})", "warning")
                unassociated.append({"block_id": b.block_id,
                                     "reason": "caption_parent_missing"})
                continue
            _attach(parent, b)
            if b.block_type == "table_note":
                parent.extra.setdefault("footnote_block_ids", []).append(b.block_id)
                b.extra["note_of"] = parent.block_id
            else:
                parent.extra["caption_block_id"] = b.block_id
                parent.extra["caption_text"] = b.normalized_text
                no = normalize_caption_no(b.normalized_text)
                if no:
                    parent.extra["caption_no"] = no
                    b.extra["caption_no"] = no
            embedded += 1
            associations.append({"subject": parent.block_id, "object": b.block_id,
                                 "type": b.block_type, "origin": "parser_embedded"})

    # 2) 邻域分离题注：无题注的图/表查找附近独立题注文本块
    reclassified: List[dict] = []
    for b in blocks:
        if b.block_type not in ("figure", "table"):
            continue
        if b.extra.get("caption_block_id"):
            continue
        if not b.bbox:
            continue
        best: Optional[Tuple[Block, float, str]] = None
        for cand in pages[b.pdf_page]:
            if cand.block_type not in ("paragraph", "list_item", "heading"):
                continue
            if cand.status != "normal":
                continue
            if cand.extra.get("associated_with"):
                continue
            text = cand.normalized_text.strip()
            if len(text) > 120 or not _CAPTION_RE.match(text):
                continue
            gap = _vertical_gap(b, cand)
            if gap is None or gap > caption_gap:
                continue
            conf = 1.0 - gap / caption_gap * (1 - min_conf)
            pos = "above" if cand.bbox[3] <= b.bbox[1] else "below"
            if best is None or conf > best[1]:
                best = (cand, conf, pos)
        if best is None:
            b.add_issue("caption_missing",
                        "未找到图题/表题（解析器未内嵌且邻域无独立题注）", "warning")
            continue
        cand, conf, pos = best
        expected_type = "figure_title" if b.block_type == "figure" else "table_title"
        cand.block_type = expected_type
        cand.add_issue("reclassified_from_paragraph",
                       f"由 {cand.original_type} 重分类为题注（邻域关联置信度 {conf:.2f}）", "info")
        _attach(b, cand)
        b.extra["caption_block_id"] = cand.block_id
        b.extra["caption_text"] = cand.normalized_text
        no = normalize_caption_no(cand.normalized_text)
        if no:
            b.extra["caption_no"] = no
            cand.extra["caption_no"] = no
        associations.append({"subject": b.block_id, "object": cand.block_id,
                             "type": expected_type,
                             "origin": f"proximity:{pos}", "confidence": round(conf, 2)})
        reclassified.append({"block_id": cand.block_id, "caption_text": cand.normalized_text,
                             "target": b.block_id, "confidence": round(conf, 2)})
        separated += 1

    return {
        "embedded_pairs": embedded,
        "separated_pairs": separated,
        "reclassified_blocks": reclassified,
        "unassociated_caption_blocks": unassociated,
        "associations": associations,
    }


def _attach(parent: Block, caption: Block) -> None:
    """把题注/脚注块绑定到视觉母块。"""
    caption.extra["associated_with"] = parent.block_id
    caption.extra["caption_of"] = parent.block_id
    parent.extra.setdefault("caption_block_ids", []).append(caption.block_id)
