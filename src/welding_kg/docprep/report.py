"""预处理报告组装（preprocessing_report.json，任务书 v2 第 4.2 节）。"""

from __future__ import annotations

from typing import Any, Dict, List

from . import PREPROCESS_VERSION


def build_meta(cfg: dict) -> dict:
    doc = cfg["document"]
    return {
        "document_id": doc["id"],
        "document_version": doc["version"],
        "source_pdf": doc["source_pdf"],
        "source_json": doc["parsed_json"],
        "parser_name": doc["parser_name"],
        "parser_version": doc["parser_version"],
        "preprocess_version": doc.get("preprocess_version", PREPROCESS_VERSION),
    }


def build_report(meta: dict, input_audit: dict, stages: Dict[str, Any],
                 quality: dict) -> dict:
    """报告结构：输入统计 / 各阶段统计 / 自动验收结果。"""
    return {
        "meta": meta,
        "input_audit": input_audit,
        "stages": stages,
        "quality": quality,
    }
