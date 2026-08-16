"""阶段 D：标题层级与章节树恢复。

- 按编号规则重新判定标题层级（不信任原解析 level 字段）；
- 处理无编号标题、跨行标题、OCR 编号粘连；
- 章节 end_source_ref/end_page 必须结束于下一个同级或更高级标题之前
  （v2 修复：原实现用对象 id 查询以 block_id 为键的映射，导致全部章节
  结束于文档最后一页）；
- 每个内容块直接绑定 section_id（最深层归属），不依赖 heading_path 反向匹配。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .model import Block, SectionNode
from .textutil import latex_to_plain, strip_latex_math

MARGIN_TYPES = {"page_header", "page_footer", "page_number", "page_aside_text"}


def normalize_title_text(raw_text: str) -> str:
    """标题规范化文本：LaTeX 转可读、空白折叠。"""
    t = strip_latex_math(raw_text)
    t = latex_to_plain(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def determine_level(title: str, cfg: dict) -> Tuple[int, str, Optional[str]]:
    """判定标题层级与自身编号。返回 (level, own_number, matched_pattern)。"""
    patterns = cfg["heading"]["level_patterns"]
    appendix_re = cfg["heading"]["appendix_pattern"]
    appendix_level = int(cfg["heading"]["appendix_level"])
    unnumbered = cfg["heading"]["unnumbered_level1"]

    t = title.strip()
    # 附录 A/B/C 挂在“附录”下
    if re.match(appendix_re, t):
        m = re.match(r"^附录\s*([A-Za-z]+)", t)
        return appendix_level, m.group(1), "appendix"
    for p in patterns:
        if re.match(p["regex"], t):
            level = int(p["level"])
            own = _extract_own_number(t, level)
            return level, own, p["regex"]
    # 无编号标题：目录/前言/序/参考文献等
    for name in unnumbered:
        if t.startswith(name):
            return 1, _slug(name), "unnumbered_level1"
    return 1, _slug(t), "unnumbered"


def _extract_own_number(title: str, level: int) -> str:
    """提取标题自身编号（用于拼接章节编号）。"""
    if level == 1:
        m = re.match(r"^第([0-9０-９一二三四五六七八九十百]+)章", title)
        if m:
            return _cn_num_to_arabic(m.group(1))
        return _slug(title)
    if level == 2:
        m = re.match(r"^(\d+)\.(\d+)", title)
        return m.group(2) if m else _slug(title)
    if level == 3:
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)", title)
        return m.group(3) if m else _slug(title)
    if level == 4:
        m = re.match(r"^(\d+)\.", title)
        return m.group(1) if m else _slug(title)
    if level == 5:
        m = re.match(r"^[（(](\d+)[）)]", title)
        return m.group(1) if m else _slug(title)
    if level == 6:
        m = re.match(r"^(\d+)[）)]", title)
        return m.group(1) if m else _slug(title)
    return _slug(title)


_CN_NUM = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}


def _cn_num_to_arabic(text: str) -> str:
    """中文数字 → 阿拉伯数字（仅处理本书涉及的简单形式）。"""
    if re.fullmatch(r"[0-9０-９]+", text):
        return text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    try:
        total = 0
        cur = 0
        for ch in text:
            if ch in _CN_NUM:
                v = _CN_NUM[ch]
                if v == 100:
                    cur = (cur or 1) * 100
                elif v == 10:
                    cur = (cur or 1) * 10
                else:
                    total += cur
                    cur = v
            elif ch.isdigit():
                total += cur
                cur = int(ch)
            else:
                return text  # 无法转换时原样返回，保证确定性
        return str(total + cur)
    except Exception:
        return re.sub(r"[^0-9]", "", text) or "0"


def _slug(text: str) -> str:
    """无编号标题 → 编号占位符（去除空白，保留可读字符）。"""
    s = re.sub(r"\s+", "", text)
    s = re.sub(r"[\\{}]", "", s)
    return s[:40] or "unnamed"


_SENTENCE_PUNCT = "。！？；，"


def split_heading_paragraphs(pages: List[List[Block]], cfg: dict) -> List[dict]:
    """修复解析器把“标题+正文”合并进同一段落块的问题。

    - 短文本（≤30 字符）且无句末标点：整块重分类为标题；
    - 编号后第一个空格前为 4~24 字符的标题名：拆分为标题块 + 正文块；
    - 其余（编号与正文粘连）：保留段落并标记待复核（任务书 v2 第 5.2 节）。
    前辅文（目录页）不处理。返回处理记录。
    """
    front_last = cfg["page"]["front_matter_pdf_pages"][1]
    records: List[dict] = []
    for page in pages:
        if page and page[0].pdf_page <= front_last:
            continue
        out: List[Block] = []
        for b in page:
            if b.block_type != "paragraph" or b.status != "normal":
                out.append(b)
                continue
            text = latex_to_plain(strip_latex_math(b.raw_text)).strip()
            if not text:
                out.append(b)
                continue
            m = _match_heading_pattern(text, cfg)
            if m is None:
                out.append(b)
                continue
            num_end = m.end()
            rest = text[num_end:].lstrip()
            rest_start = num_end + (len(text[num_end:]) - len(rest))

            # 规则 2：整块是短标题（全文本无句末标点、无尾随页码）
            if (len(text) <= 30 and not re.search(r"[。！？；，]", text)
                    and not re.search(r"\d{2,4}$", text)):
                b.block_type = "heading"
                b.extra["split_title"] = text
                b.add_issue("reclassified_from_paragraph",
                            "短文本且以标题编号开头，整块重分类为标题", "info")
                records.append({"type": "reclassify_whole", "block_id": b.block_id,
                                "title": text})
                out.append(b)
                continue

            # 规则 1：编号后第一个空格边界拆分
            # 标题名须 4~24 字符且不含句末标点（防止把正文句误拆成标题）
            space_idx = rest.find(" ")
            if 0 < space_idx <= 24 and len(rest) > space_idx + 4:
                title_part = (text[:rest_start] + rest[:space_idx]).strip()
                body_part = rest[space_idx + 1:].strip()
                title_name = title_part[num_end:].strip()
                if (len(title_name) >= 4 and body_part
                        and not re.search(r"[。！？；，]", title_part)):
                    b.block_type = "heading"
                    b.extra["split_title"] = title_part
                    b.add_issue("split_heading_from_paragraph",
                                "标题与正文合并块拆分：正文另立新块", "info")
                    body = Block(
                        block_id="", block_type="paragraph",
                        pdf_page=b.pdf_page, printed_page=b.printed_page,
                        printed_page_numeric=b.printed_page_numeric,
                        block_order=-1, bbox=list(b.bbox), raw_text=b.raw_text,
                        normalized_text="", heading_path=[],
                        source_ref="", status="normal",
                        issues=[{"code": "split_from_merged_block",
                                 "message": f"由块 {b.block_id} 拆分出（标题+正文合并）",
                                 "severity": "info"}],
                        original_type=b.original_type, original_index=b.original_index,
                        extra={"split_from": b.block_id, "split_body": body_part},
                    )
                    records.append({"type": "split", "block_id": b.block_id,
                                    "title": title_part, "body_prefix": body_part[:20]})
                    out.append(b)
                    out.append(body)
                    continue
            # 规则 3：粘连无法可靠拆分 → 标记待复核（不得把整段正文写入标题）
            b.status = "needs_review"
            b.add_issue("possible_heading_in_paragraph",
                        "段落以标题编号开头但无可靠边界，疑似标题与正文粘连，待复核",
                        "warning")
            records.append({"type": "flag", "block_id": b.block_id, "text_prefix": text[:24]})
            out.append(b)
        page[:] = out
    return records


def _match_heading_pattern(text: str, cfg: dict):
    """段落首部是否匹配标题编号模式（排除“第X章”，那必然是标题块）。"""
    for p in cfg["heading"]["level_patterns"]:
        m = re.match(p["regex"], text)
        if m:
            return m
    return None


def merge_crossline_headings(pages: List[List[Block]], cfg: dict) -> List[dict]:
    """同页相邻标题跨行合并。返回合并记录。"""
    gap = cfg["heading"]["cross_line_gap"]
    records: List[dict] = []
    for page in pages:
        for i in range(len(page) - 1):
            a, b = page[i], page[i + 1]
            if a.block_type != "heading" or b.block_type != "heading":
                continue
            if a.status != "normal" or b.status != "normal":
                continue
            ta = normalize_title_text(a.raw_text)
            tb = normalize_title_text(b.raw_text)
            # 两个标题都无编号且纵向紧邻 → 视为同一标题的两行
            if (determine_level(ta, cfg)[0] == 1 and determine_level(tb, cfg)[0] == 1
                    and a.bbox and b.bbox and b.bbox[1] - a.bbox[3] < gap):
                merged_text = f"{ta} {tb}"
                b.extra["merged_from"] = [a.block_id]
                b.extra["merged_raw_text"] = f"{a.raw_text} {b.raw_text}"
                a.status = "merged"
                a.add_issue("merged_into", f"跨行标题，并入 {b.block_id}", "info")
                records.append({"first": a.block_id, "second": b.block_id,
                                "merged_text": merged_text})
    return records


def flag_heading_body_glue(pages: List[List[Block]]) -> List[dict]:
    """标记标题块粘连正文首行的情况（如标题尾接正文片段、下一段以残句开头）。"""
    records: List[dict] = []
    flat = [b for page in pages for b in page]
    for i, b in enumerate(flat):
        if b.block_type != "heading" or b.status != "normal":
            continue
        if b.extra.get("split_title"):
            continue  # 已由拆分器处理
        nxt = flat[i + 1] if i + 1 < len(flat) else None
        if nxt is None or nxt.block_type != "paragraph" or not nxt.normalized_text:
            continue
        # 下一段以 1-2 字残句开头（如“解，由于…”）→ 标题与正文边界可疑
        first_seg = re.split(r"[，。；！？]", nxt.normalized_text)[0]
        if 1 <= len(first_seg.strip()) <= 2:
            b.status = "needs_review"  # 无法可靠拆分：标记待复核
            b.add_issue("heading_body_boundary_suspect",
                        f"标题后段落以残句开头（{first_seg.strip()!r}），"
                        "标题与正文边界无法可靠拆分，标记待复核",
                        "warning")
            nxt.add_issue("starts_with_sentence_fragment",
                          f"段落以残句开头，疑似承接标题 {b.block_id} 的正文首行",
                          "info")
            records.append({"heading_block_id": b.block_id,
                            "paragraph_block_id": nxt.block_id,
                            "fragment": first_seg.strip()})
    return records


def build_sections(pages: List[List[Block]], cfg: dict) -> Tuple[List[SectionNode], List[dict]]:
    """构建章节树、为所有块绑定 section_id 与 heading_path。

    返回 (节点列表含根节点, 问题记录)。根节点 section_id 为 gmaw:v2:sec:root。

    章节范围：end 结束于下一个同级或更高级标题之前（范围内最后一个
    非版边块）；修复：标题节点查询统一用 block_id 字符串键。
    """
    front_prefix = cfg["heading"]["front_matter_prefix"]
    front_last = cfg["page"]["front_matter_pdf_pages"][1]

    root = SectionNode(
        section_id="gmaw:v2:sec:root", title="", normalized_title="", level=0,
        parent_section_id=None, start_source_ref="", end_source_ref=None,
        start_page=None, end_page=None, number="", heading_block_id="",
    )
    nodes: List[SectionNode] = [root]
    used_numbers: Dict[str, int] = {}
    jump_flags: List[dict] = []

    # 文档顺序的块列表（含 merged/needs_review 块，供路径继承与章节边界定位）
    flat: List[Block] = [b for page in pages for b in page]

    heading_blocks: List[Block] = []
    for b in flat:
        if b.block_type == "heading" and b.status in ("normal", "needs_review"):
            if b.extra.get("split_title"):
                # 标题字段只保存拆分后的标题，不得包含正文
                b.normalized_text = normalize_title_text(b.extra["split_title"])
            else:
                b.normalized_text = normalize_title_text(b.raw_text)
            heading_blocks.append(b)
    # 块位置（按 block_id 键，保证与 heading_node 键一致）
    flat_pos: Dict[str, int] = {b.block_id: i for i, b in enumerate(flat)}

    # 逐标题建节点
    node_of: Dict[str, SectionNode] = {root.section_id: root}
    heading_node: Dict[str, SectionNode] = {}
    prev_by_level: Dict[int, SectionNode] = {0: root}

    for hb in heading_blocks:
        title = hb.normalized_text
        level, own, matched = determine_level(title, cfg)
        hb.extra["heading_level"] = level
        hb.extra["heading_number"] = own
        hb.extra["heading_pattern"] = matched

        parent = None
        for lv in range(level - 1, -1, -1):
            if lv in prev_by_level:
                parent = prev_by_level[lv]
                break
        if parent is None:
            parent = root

        # 组装章节编号
        if level == 1:
            if hb.pdf_page <= front_last:
                number = f"{front_prefix}{own}"
            else:
                number = own
        else:
            pnum = parent.number
            base = pnum.split(":", 1)[-1] if pnum.startswith(front_prefix) else pnum
            if parent.section_id == root.section_id:
                number = own
            else:
                number = f"{base}.{own}"

        # 唯一化
        if number in used_numbers:
            used_numbers[number] += 1
            unique_number = f"{number}-{used_numbers[number]}"
        else:
            used_numbers[number] = 1
            unique_number = number

        section_id = f"gmaw:v2:sec:{unique_number}"
        node = SectionNode(
            section_id=section_id, title=hb.raw_text, normalized_title=title,
            level=level, parent_section_id=parent.section_id,
            start_source_ref=hb.source_ref, end_source_ref=None,
            start_page=hb.pdf_page, end_page=None, number=unique_number,
            heading_block_id=hb.block_id,
        )
        # 层级跳跃标记（如 2 级直接到 4 级）
        if parent.level > 0 and level > parent.level + 1 and cfg["heading"].get("level_jump_flag", True):
            jump_flags.append({
                "code": "heading_level_jump",
                "block_id": hb.block_id,
                "message": f"标题层级从 {parent.level} 跳到 {level}（{title}），按最近低层级归父",
                "severity": "warning",
            })
            node.issues.append({"code": "heading_level_jump", "message": jump_flags[-1]["message"]})
            hb.add_issue("heading_level_jump", jump_flags[-1]["message"], "warning")

        nodes.append(node)
        node_of[section_id] = node
        heading_node[hb.block_id] = node
        parent.children.append(section_id)
        prev_by_level[level] = node
        # 清除更深层级的“上一个”记忆，保证同级/更深标题挂在当前节点下
        for lv in range(level + 1, 7):
            prev_by_level.pop(lv, None)

        hb.extra["section_id"] = section_id
        hb.heading_path = _chain_for(node, heading_node, node_of, root)

    # 章节 end_source_ref / end_page：结束于下一个 level <= 自身 level 的标题之前
    # （v2 修复：用 hb2.block_id 查询 heading_node；原实现误用对象 id，永不命中）
    for idx, hb in enumerate(heading_blocks):
        node = heading_node[hb.block_id]
        pos = flat_pos[hb.block_id]
        end_pos = len(flat)
        for hb2 in heading_blocks[idx + 1:]:
            if hb2.block_id in heading_node:
                n2 = heading_node[hb2.block_id]
                if n2.level <= node.level:
                    end_pos = flat_pos[hb2.block_id]
                    break
        # 范围内最后一个非版边块作为 end（避免 end_source_ref 落在页眉/页码上）
        end_block = None
        for b in reversed(flat[pos:end_pos]):
            if b.block_type not in MARGIN_TYPES:
                end_block = b
                break
        if end_block is not None and end_block is not hb:
            node.end_source_ref = end_block.source_ref
            node.end_page = end_block.pdf_page
        else:
            node.end_source_ref = node.start_source_ref
            node.end_page = node.start_page

    # heading_path 与 section_id 绑定（文档顺序单次遍历，含 merged/needs_review 块）
    chain_by_level: Dict[int, List[str]] = {0: []}
    current_section: SectionNode = root
    for b in flat:
        if b.block_type == "heading" and b.status in ("normal", "needs_review"):
            node = heading_node[b.block_id]
            parent_chain = chain_by_level.get(node.level - 1, [])
            if node.parent_section_id == root.section_id:
                parent_chain = []
            chain = parent_chain + [node.normalized_title]
            chain_by_level[node.level] = chain
            for lv in range(node.level + 1, 7):
                chain_by_level.pop(lv, None)
            b.heading_path = chain
            node.heading_path = list(chain)
            current_section = node
        else:
            chain = []
            for lv in range(6, -1, -1):
                if lv in chain_by_level:
                    chain = chain_by_level[lv]
                    break
            b.heading_path = list(chain)
            b.section_id = current_section.section_id

    return nodes, jump_flags


def _chain_for(node: SectionNode, heading_node: Dict[str, SectionNode],
               node_of: Dict[str, SectionNode], root: SectionNode) -> List[str]:
    """从根到该节点的标题路径。"""
    chain: List[str] = []
    cur: Optional[SectionNode] = node
    while cur is not None and cur.section_id != root.section_id:
        chain.append(cur.normalized_title)
        cur = node_of.get(cur.parent_section_id) if cur.parent_section_id else None
    return list(reversed(chain))
