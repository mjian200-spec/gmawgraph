"""阶段 C：页码与内容顺序恢复。

- 印刷页码映射（罗马/阿拉伯、缺页推断、异常记录）；
- 页面内阅读顺序（栏检测 + 版面坐标，保留原顺序供核对）；
- 分配 block_id 与 source_ref。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .audit import classify_printed_page, roman_to_int
from .model import Block

MARGIN_TYPES = {"page_header", "page_footer", "page_number", "page_aside_text", "page_footnote"}


def build_printed_page_map(pages: List[List[Block]], cfg: dict) -> Tuple[Dict[int, dict], List[dict]]:
    """构建 pdf_page → 印刷页码映射。返回 (映射, 异常列表)。

    映射值: {"class": "arabic"|"roman"|None, "value": 规范化字符串, "numeric": int|None,
             "inferred": bool, "source_blocks": [原始页码块文本]}
    """
    anchors: Dict[int, dict] = {}
    anomalies: List[dict] = []

    for p_idx, page in enumerate(pages):
        candidates = []
        for b in page:
            if b.block_type == "page_number":
                cat, val = classify_printed_page(b.raw_text)
                candidates.append((b, cat, val))
        if not candidates:
            continue
        # 优先取位于页面底部的页码块；同一页多个时取最靠下的
        candidates.sort(key=lambda t: t[0].bbox[1] if t[0].bbox else 0, reverse=True)
        primary = candidates[0]
        cat, val = primary[1], primary[2]
        numeric = None
        if cat == "arabic":
            numeric = int(val)
        elif cat == "roman":
            numeric = roman_to_int(val)
        anchors[p_idx] = {
            "class": cat,
            "value": val,
            "numeric": numeric,
            "inferred": False,
            "source_blocks": [b.raw_text.strip() for b, _, _ in candidates],
        }
        if len(candidates) > 1 and len({c[1] for c in candidates}) > 1:
            anomalies.append({"code": "multi_pagenum_class_conflict",
                              "pdf_page": p_idx,
                              "message": f"同页多个页码块类别不一致: {[c[2] for c in candidates]}"})
        if cat is None:
            anomalies.append({"code": "pagenum_unrecognized",
                              "pdf_page": p_idx,
                              "message": f"页码无法识别: {b.raw_text!r}"})

    # 类别切换检测
    classes = [(p, a["class"]) for p, a in sorted(anchors.items()) if a["class"]]
    transitions = []
    for i in range(1, len(classes)):
        if classes[i][1] != classes[i - 1][1]:
            transitions.append({"pdf_page": classes[i][0], "from": classes[i - 1][1],
                                "to": classes[i][1]})
    if transitions:
        anomalies.append({"code": "pagenum_class_transition", "transitions": transitions,
                          "message": "页码类别切换（如罗马→阿拉伯）"})

    # 阿拉伯页码线性推断补齐缺失页
    arabic_anchors = sorted((p, a["numeric"]) for p, a in anchors.items()
                            if a["class"] == "arabic" and a["numeric"] is not None)
    if len(arabic_anchors) >= 2:
        i = 0
        while i < len(arabic_anchors) - 1:
            p0, n0 = arabic_anchors[i]
            p1, n1 = arabic_anchors[i + 1]
            gap_pages = p1 - p0
            gap_nums = n1 - n0
            if gap_pages > 1:
                if gap_nums == gap_pages:  # 线性吻合才推断
                    for p in range(p0 + 1, p1):
                        anchors[p] = {"class": "arabic", "value": str(n0 + (p - p0)),
                                      "numeric": n0 + (p - p0), "inferred": True,
                                      "source_blocks": []}
                else:
                    anomalies.append({"code": "pagenum_gap_nonlinear",
                                      "pdf_pages": [p0, p1],
                                      "message": f"页码 {n0}→{n1} 与页数差 {gap_pages} 不符，不推断"})
            i += 1

    # 重复/回退检测
    seen: Dict[int, int] = {}
    for p in sorted(anchors):
        n = anchors[p]["numeric"]
        if n is None:
            continue
        if n in seen:
            anomalies.append({"code": "pagenum_duplicate",
                              "pdf_pages": [seen[n], p],
                              "message": f"印刷页码 {n} 在 pdf 第 {seen[n]} 页与第 {p} 页重复"})
        else:
            seen[n] = p
    nums = [anchors[p]["numeric"] for p in sorted(anchors) if anchors[p]["numeric"] is not None]
    for i in range(1, len(nums)):
        if nums[i] < nums[i - 1]:
            anomalies.append({"code": "pagenum_regression",
                              "message": f"印刷页码回退: {nums[i - 1]} → {nums[i]}"})
            break

    return anchors, anomalies


def _detect_columns(blocks: List[Block], cfg: dict) -> Tuple[List[List[Block]], bool]:
    """把页内内容块按栏分组。返回 (按阅读顺序排列的栏列表, 是否需要版面复核)。

    内容块 = 非页眉页脚页码等版边块。按 x 中心聚类，间隙超过阈值视为分栏。
    """
    content = [b for b in blocks if b.block_type not in MARGIN_TYPES]
    if not content:
        return [], False
    gap = cfg["page"].get("column_gap", 250)
    centers = sorted((((b.bbox[0] + b.bbox[2]) / 2 if b.bbox else 0), i) for i, b in enumerate(content))
    clusters: List[List[int]] = []
    cur = [centers[0][1]]
    for j in range(1, len(centers)):
        if centers[j][0] - centers[j - 1][0] > gap:
            clusters.append(cur)
            cur = []
        cur.append(centers[j][1])
    clusters.append(cur)

    needs_review = False
    if len(clusters) > 2:
        needs_review = True   # 复杂版式，不硬判
        return [content], True
    if len(clusters) == 2:
        # 只有两个栏都有实际内容宽度且数量相当才认可分栏
        if len(clusters[0]) >= 2 and len(clusters[1]) >= 2:
            # 按 x 排序：左栏在前
            left = clusters[0] if centers[clusters[0][0]][0] < centers[clusters[1][0]][0] else clusters[1]
            right = clusters[1] if left == clusters[0] else clusters[0]
            return [[content[i] for i in left], [content[i] for i in right]], True
        needs_review = True
    # 单栏或不成栏：按版式排序
    return [content], needs_review


def paginate(pages: List[List[Block]], cfg: dict) -> dict:
    """执行阶段 C：页码映射 + 页面内排序 + id 分配。原地修改 Block。"""
    code_map = cfg["source_ref_codes"]
    printed_map, pagenum_anomalies = build_printed_page_map(pages, cfg)
    layout_review_pages: List[int] = []
    id_seen: set = set()
    ref_seen: set = set()

    for p_idx, page in enumerate(pages):
        pm = printed_map.get(p_idx)
        printed_val = pm["value"] if pm else None
        printed_num = pm["numeric"] if pm else None

        for b in page:
            b.printed_page = printed_val
            b.printed_page_numeric = printed_num

        columns, review = _detect_columns(page, cfg)
        if review:
            layout_review_pages.append(p_idx)

        ordered: List[Block] = []
        for col in columns:
            col.sort(key=lambda b: (b.bbox[1] if b.bbox else 0,
                                    b.bbox[0] if b.bbox else 0,
                                    b.original_index))
            ordered.extend(col)
        # 版边块（页眉/页脚/页码/边注）排在内容之后，按 y 排序，仅作占位
        margin = [b for b in page if b.block_type in MARGIN_TYPES]
        margin.sort(key=lambda b: (b.bbox[1] if b.bbox else 0, b.original_index))
        ordered.extend(margin)

        type_counters: Dict[str, int] = {}
        for order, b in enumerate(ordered):
            b.block_order = order
            bid = f"gmaw:v2:pdfp{p_idx:04d}:b{order:03d}"
            if bid in id_seen:
                raise RuntimeError(f"block_id 冲突: {bid}")
            id_seen.add(bid)
            b.block_id = bid

            code = code_map.get(b.block_type, "unk")
            seq = type_counters.get(code, 0) + 1
            type_counters[code] = seq
            sref = f"GMAW:v2:pdfp{p_idx:04d}:{code}{seq:02d}"
            if sref in ref_seen:
                raise RuntimeError(f"source_ref 冲突: {sref}")
            ref_seen.add(sref)
            b.source_ref = sref

        pages[p_idx] = ordered

    # 印刷页码未识别的页
    no_page = [p for p in range(len(pages)) if p not in printed_map]
    summary = {
        "printed_page_anomalies": pagenum_anomalies,
        "pages_without_printed_page": no_page,
        "layout_review_pages": layout_review_pages,
        "layout_review_count": len(layout_review_pages),
        "printed_page_anchor_count": len(printed_map),
    }
    return summary
