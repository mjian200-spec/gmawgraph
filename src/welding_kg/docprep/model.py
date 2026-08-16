"""核心数据模型：标准化内容块、章节节点、有序内容项。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class Block:
    """标准化物理内容块（任务书 v2 第 5.2 节）。

    raw_text 为原始 OCR 文本，永不修改；清洗只写入 normalized_text。
    section_id 为最深层章节归属，直接保存，不依赖 heading_path 反向匹配。
    """

    block_id: str                          # gmaw:v2:pdfp0294:b002
    block_type: str                        # heading/paragraph/table/... 见任务书 5.2
    pdf_page: int                          # PDF 物理页码（0 起）
    printed_page: Optional[str]            # 书本印刷页码（不可识别为 null）
    printed_page_numeric: Optional[int]    # 印刷页码数值（罗马数字换算；null）
    block_order: int                       # 页面内阅读顺序（0 起）
    bbox: list                             # [x0, y0, x1, y1]
    raw_text: str                          # 原始 OCR 文本，绝不修改
    normalized_text: str                   # 清洗后的文本
    heading_path: list                     # 从顶级章节到当前块的标题路径
    source_ref: str                        # 图谱外部溯源编码
    status: str                            # normal/merged/needs_review/excluded
    issues: list = field(default_factory=list)   # [{code, message, severity}]
    original_type: str = ""                # 原始解析类型
    original_index: int = -1               # 原始页面内数组下标
    section_id: str = ""                   # 最深层章节归属（阶段 D 写入）
    extra: dict = field(default_factory=dict)    # 类型特有字段（latex/ocr_html/图片路径等）

    def to_json_obj(self) -> dict:
        """按固定字段顺序输出 JSON 对象（保证确定性）。"""
        obj = {
            "block_id": self.block_id,
            "block_type": self.block_type,
            "pdf_page": self.pdf_page,
            "printed_page": self.printed_page,
            "block_order": self.block_order,
            "bbox": self.bbox,
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "heading_path": self.heading_path,
            "source_ref": self.source_ref,
            "status": self.status,
            "issues": self.issues,
        }
        obj["printed_page_numeric"] = self.printed_page_numeric
        obj["original_type"] = self.original_type
        obj["original_index"] = self.original_index
        obj["section_id"] = self.section_id
        for k, v in self.extra.items():
            obj[k] = v
        return obj

    @classmethod
    def from_json_obj(cls, obj: dict) -> "Block":
        """从 JSON 对象重建（非标准字段落入 extra，保证对称往返）。"""
        std = {k: v for k, v in obj.items() if k in cls.__dataclass_fields__}
        std.setdefault("issues", [])
        extra = {k: v for k, v in obj.items() if k not in cls.__dataclass_fields__}
        if extra:
            std["extra"] = extra
        return cls(**std)

    def add_issue(self, code: str, message: str, severity: str = "info") -> None:
        self.issues.append({"code": code, "message": message, "severity": severity})


@dataclass
class ContentItem:
    """章节内容流中的一个有序项（任务书 v2 第 4.1 节）。

    item_type: text_segment | figure | table | subsection。
    - text_segment: text + member_block_ids + source_refs + sources；
    - figure/table: 资产记录（asset_path 等）+ caption/footnotes + 溯源；
    - subsection: section_id 指向子章节（item_id 即子 section_id）。
    """

    item_type: str
    item_id: str
    order: int
    section_id: str = ""            # 所属章节（最深层）
    heading_path: list = field(default_factory=list)
    # text_segment
    text: str = ""
    member_block_ids: list = field(default_factory=list)
    source_refs: list = field(default_factory=list)
    sources: list = field(default_factory=list)   # [{pdf_page, bbox, source_ref}]
    # figure / table
    asset_path: str = ""
    original_asset_path: str = ""
    asset_origin: str = ""          # parsed_asset | pdf_crop
    asset_exists: bool = False
    sha256: str = ""
    mime_type: str = ""
    caption: str = ""
    footnotes: list = field(default_factory=list)
    ocr_html: str = ""              # table 仅辅助字段，非原图替代品
    pdf_page: Optional[int] = None
    bbox: list = field(default_factory=list)
    source_ref: str = ""
    issues: list = field(default_factory=list)

    def to_json_obj(self) -> dict:
        obj: dict = {"item_type": self.item_type, "item_id": self.item_id,
                     "order": self.order}
        if self.item_type == "subsection":
            obj["section_id"] = self.item_id
            return obj
        obj["section_id"] = self.section_id
        obj["heading_path"] = self.heading_path
        if self.item_type == "text_segment":
            obj["text"] = self.text
            obj["member_block_ids"] = self.member_block_ids
            obj["source_refs"] = self.source_refs
            obj["sources"] = self.sources
        else:  # figure / table
            obj["asset_path"] = self.asset_path
            obj["original_asset_path"] = self.original_asset_path
            obj["asset_origin"] = self.asset_origin
            obj["asset_exists"] = self.asset_exists
            obj["sha256"] = self.sha256
            obj["mime_type"] = self.mime_type
            obj["caption"] = self.caption
            obj["footnotes"] = self.footnotes
            if self.item_type == "table":
                obj["ocr_html"] = self.ocr_html
            obj["pdf_page"] = self.pdf_page
            obj["bbox"] = self.bbox
            obj["source_ref"] = self.source_ref
        if self.issues:
            obj["issues"] = self.issues
        return obj

    @classmethod
    def from_json_obj(cls, obj: dict) -> "ContentItem":
        fields = {k: v for k, v in obj.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class SectionNode:
    """章节树节点（任务书 v2 第 4.1 节）。

    title 只保存拆分后的标题文本；content 为该标题范围内的有序内容流，
    子章节内容只保存在子节点中，父节点以 subsection 项引用。
    """

    section_id: str
    title: str                       # 原始标题（含 LaTeX 的原样拼接，仅标题部分）
    normalized_title: str            # 规范化标题（仅标题部分，不含正文）
    level: int
    parent_section_id: Optional[str]
    start_source_ref: str
    end_source_ref: Optional[str]
    start_page: int
    end_page: Optional[int]
    number: str                      # 编号或简称，用于生成 section_id
    heading_block_id: str
    heading_path: list = field(default_factory=list)
    children: list = field(default_factory=list)   # 子节点 id（排序后输出）
    content: list = field(default_factory=list)    # ContentItem 列表（有序）
    issues: list = field(default_factory=list)

    def to_json_obj(self) -> dict:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "normalized_title": self.normalized_title,
            "level": self.level,
            "parent_section_id": self.parent_section_id,
            "start_source_ref": self.start_source_ref,
            "end_source_ref": self.end_source_ref,
            "start_page": self.start_page,
            "end_page": self.end_page,
            "number": self.number,
            "heading_block_id": self.heading_block_id,
            "heading_path": self.heading_path,
            "children": sorted(self.children),
            "content": [i.to_json_obj() for i in self.content],
            "issues": self.issues,
        }

    @classmethod
    def from_json_obj(cls, obj: dict) -> "SectionNode":
        fields = {k: v for k, v in obj.items() if k in cls.__dataclass_fields__}
        fields["content"] = [ContentItem.from_json_obj(i) if isinstance(i, dict) else i
                             for i in fields.get("content", [])]
        return cls(**fields)
