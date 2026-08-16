"""确定性输出写入：原子写（临时文件 + rename）、UTF-8、固定字段顺序。"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, List

from .model import Block, SectionNode


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_document_structure(path: str, sections: List[SectionNode], meta: dict) -> None:
    """主输出：document_structure.json（任务书 v2 第 4.1 节）。

    sections 按 section_order 平铺；根节点在首位；每个节点含 content 有序流。
    """
    # 根节点（start_page=None）在最前，其余按文档序（start_page, section_id）
    ordered = sorted(sections, key=lambda s: (s.start_page is not None,
                                              s.start_page or 0,
                                              s.section_id))
    obj = {
        "meta": meta,
        "sections": [s.to_json_obj() for s in ordered],
        "section_order": [s.section_id for s in ordered],
    }
    _atomic_write(path, _json_dump(obj))


def write_source_registry(path: str, blocks: List[Block], sections: List[SectionNode],
                          meta: dict) -> None:
    """source_ref → PDF 位置映射；figure/table 项还必须包含可用 asset_path。"""
    registry: dict = {}
    for b in blocks:
        entry = {
            "block_id": b.block_id,
            "block_type": b.block_type,
            "pdf_page": b.pdf_page,
            "printed_page": b.printed_page,
            "printed_page_numeric": b.printed_page_numeric,
            "bbox": b.bbox,
            "section_id": b.section_id,
        }
        if b.block_type in ("figure", "table"):
            asset = b.extra.get("asset", {})
            entry["asset_path"] = asset.get("asset_path", "")
            entry["asset_origin"] = asset.get("asset_origin", "")
        registry[b.source_ref] = entry
    obj = {
        "meta": meta,
        "registry": {k: registry[k] for k in sorted(registry)},
    }
    _atomic_write(path, _json_dump(obj))


def write_normalized_blocks(path: str, blocks: List[Block]) -> None:
    """内部中间结果（审计与溯源用，非主接口）。"""
    ordered = sorted(blocks, key=lambda b: (b.pdf_page, b.block_order))
    lines = [json.dumps(b.to_json_obj(), ensure_ascii=False, sort_keys=True)
             for b in ordered]
    _atomic_write(path, "\n".join(lines) + "\n")


def write_report(path: str, report: dict) -> None:
    _atomic_write(path, _json_dump(report))
