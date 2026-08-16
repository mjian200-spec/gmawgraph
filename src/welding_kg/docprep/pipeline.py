"""流水线编排（任务书 v2 第 1.2 节处理链路）：audit / run / validate 三种模式。"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

from . import PREPROCESS_VERSION
from .associate import associate
from .assets import build_asset_record
from .audit import audit
from .headings import (build_sections, flag_heading_body_glue,
                       merge_crossline_headings, split_heading_paragraphs)
from .loader import count_pdf_pages, load_content_list_v2
from .model import Block, SectionNode
from .normalize import normalize_pages
from .pagination import paginate
from .report import build_meta, build_report
from .structure import build_content_sequence, build_section_content
from .text_clean import clean_pages
from .validate import check_raw_preserved, validate_outputs
from . import writers

log = logging.getLogger("gmaw_preprocess")


class PipelineError(RuntimeError):
    """流水线失败：任何阶段失败都不得产出看似完整的最终结果。"""


def _resolve(base_dir: str, p: str) -> str:
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(base_dir, p))


def run_audit(cfg: dict, base_dir: str, write: bool = True) -> dict:
    """审计模式：只读审计 + 写报告（不生成任何补裁资源）。"""
    doc = cfg["document"]
    parsed_path = _resolve(base_dir, doc["parsed_json"])
    if not os.path.isfile(parsed_path):
        raise PipelineError(f"解析 JSON 缺失，停止审计: {parsed_path}")
    pages = load_content_list_v2(parsed_path)
    pdf_path = _resolve(base_dir, doc["source_pdf"]) if doc.get("source_pdf") else None
    pdf_pages, pdf_method = count_pdf_pages(pdf_path)
    ocr_json_dir = os.path.dirname(parsed_path)
    result = audit(pages, cfg, pdf_path, pdf_pages, pdf_method, ocr_json_dir)
    meta = build_meta(cfg)
    report = build_report(meta, result, {}, {"checks": [], "pass_count": 0,
                                             "fail_count": 0, "acceptance": {}})
    if write:
        out_path = _resolve(base_dir, cfg["paths"]["output_dir"])
        writers.write_report(os.path.join(out_path, "preprocessing_report.json"), report)
        log.info("审计完成: %s 页 JSON，报告已写入 data/docprep/preprocessing_report.json", len(pages))
    return report


def run_pipeline(cfg: dict, base_dir: str) -> dict:
    """完整预处理（任务书 v2 处理链路）。失败时不发布最终结果。"""
    doc = cfg["document"]
    log.info("=== 启动检查 ===")
    parsed_path = _resolve(base_dir, doc["parsed_json"])
    if not os.path.isfile(parsed_path):
        raise PipelineError(f"原始解析 JSON 缺失，任务阻塞: {parsed_path}")
    pdf_path = _resolve(base_dir, doc["source_pdf"]) if doc.get("source_pdf") else None
    if not pdf_path or not os.path.isfile(pdf_path):
        # 任务书 v2 第 3 节：原始 PDF 缺失必须停止（PDF 补裁与溯源依赖它）
        raise PipelineError(f"原始 PDF 缺失，任务阻塞（PDF 补裁与溯源必须来自原 PDF）: {pdf_path}")

    out_dir = _resolve(base_dir, cfg["paths"]["output_dir"])
    assets_dir = _resolve(base_dir, cfg["paths"]["assets_dir"])
    ocr_json_dir = os.path.dirname(parsed_path)
    # asset_path 以项目根（config/ 的父目录，即 GMAWGraph 根）为基准，
    # 与 README/check 脚本/validate 的消费约定一致
    project_dir = os.path.dirname(os.path.normpath(base_dir))
    stages: Dict[str, dict] = {}

    log.info("=== 阶段 A: 内容块审计 ===")
    pages_raw = load_content_list_v2(parsed_path)
    pdf_pages, pdf_method = count_pdf_pages(pdf_path)
    input_audit = audit(pages_raw, cfg, pdf_path, pdf_pages, pdf_method, ocr_json_dir)

    log.info("=== 阶段 B: 内容块标准化 ===")
    pages = normalize_pages(pages_raw, cfg)
    split_records = split_heading_paragraphs(pages, cfg)
    stages["normalize"] = {
        "ok": True,
        "summary": {
            "heading_paragraph_splits": {
                "total": len(split_records),
                "split": sum(1 for r in split_records if r["type"] == "split"),
                "reclassify_whole": sum(1 for r in split_records if r["type"] == "reclassify_whole"),
                "flagged": sum(1 for r in split_records if r["type"] == "flag"),
            },
        },
    }

    log.info("=== 阶段 C: 页码与阅读顺序恢复 ===")
    pag_summary = paginate(pages, cfg)
    stages["pagination"] = {"ok": True, "summary": pag_summary}

    log.info("=== 阶段 D: 标题层级与章节树 ===")
    merge_records = merge_crossline_headings(pages, cfg)
    sections, jump_flags = build_sections(pages, cfg)
    level_dist: Dict[int, int] = {}
    for s in sections:
        if s.level > 0:
            level_dist[s.level] = level_dist.get(s.level, 0) + 1
    stages["headings"] = {
        "ok": True,
        "summary": {
            "section_count": len(sections) - 1,  # 不含根节点
            "level_distribution": level_dist,
            "cross_line_heading_merges": len(merge_records),
            "level_jump_flags": len(jump_flags),
        },
    }

    log.info("=== 阶段 E: 正文清洗 ===")
    clean_stats = clean_pages(pages, cfg)
    glue_records = flag_heading_body_glue(pages)
    stages["text_clean"] = {
        "ok": True,
        "summary": {"applied_rules": clean_stats,
                    "heading_body_boundary_suspects": len(glue_records)},
    }

    log.info("=== 阶段 F: 视觉资源解析与补裁 ===")
    blocks_flat = [b for page in pages for b in page]
    assets_by_ref: Dict[str, dict] = {}
    asset_stats = {"parsed_asset": 0, "pdf_crop": 0, "failed": 0}
    for b in blocks_flat:
        if b.block_type not in ("figure", "table"):
            continue
        rec, is_crop = build_asset_record(b, cfg, ocr_json_dir, pdf_path,
                                          assets_dir, project_dir)
        b.extra["asset"] = rec
        assets_by_ref[b.source_ref] = rec
        if is_crop:
            if rec.get("asset_exists"):
                asset_stats["pdf_crop"] += 1
            else:
                asset_stats["failed"] += 1
        else:
            asset_stats["parsed_asset"] += 1
    stages["assets"] = {"ok": True, "summary": asset_stats}

    log.info("=== 阶段 G: 图题/表题/脚注关联 ===")
    assoc = associate(pages, cfg)
    # 脚注文本挂到视觉块
    by_id = {b.block_id: b for b in blocks_flat}
    for b in blocks_flat:
        if b.block_type in ("figure", "table"):
            foot_ids = b.extra.get("footnote_block_ids", [])
            b.extra["footnotes"] = [
                by_id[fid].normalized_text for fid in foot_ids
                if fid in by_id and by_id[fid].normalized_text.strip()]
    stages["association"] = {
        "ok": True,
        "summary": {"embedded_pairs": assoc["embedded_pairs"],
                    "separated_pairs": assoc["separated_pairs"],
                    "reclassified_blocks": len(assoc["reclassified_blocks"]),
                    "unassociated_caption_blocks": len(assoc["unassociated_caption_blocks"])},
    }

    log.info("=== 阶段 H: 章节有序内容与文本融合 ===")
    seq, seq_stats = build_content_sequence(pages, cfg)
    struct_stats, struct_issues = build_section_content(sections, seq, cfg, assets_by_ref)
    stages["structure"] = {"ok": True,
                           "summary": dict(seq_stats, **struct_stats),
                           "issues": struct_issues}

    log.info("=== 自动验证 ===")
    raw_violations = check_raw_preserved(blocks_flat, pages_raw)
    quality = validate_outputs(blocks_flat, sections, seq, cfg, raw_violations)
    blockers = [{"check": c["check"], "level": c.get("level"), "detail": c["detail"]}
                for c in quality["checks"] if not c["ok"]]
    quality["acceptance_blockers"] = blockers
    quality["acceptance_blockers_count"] = len(blockers)

    log.info("=== 输出标准化文件 ===")
    meta = build_meta(cfg)
    writers.write_document_structure(os.path.join(out_dir, "document_structure.json"),
                                     sections, meta)
    writers.write_source_registry(os.path.join(out_dir, "source_registry.json"),
                                  blocks_flat, sections, meta)
    writers.write_normalized_blocks(os.path.join(out_dir, "normalized_blocks.jsonl"),
                                    blocks_flat)
    report = build_report(meta, input_audit, stages, quality)
    writers.write_report(os.path.join(out_dir, "preprocessing_report.json"), report)
    log.info("全部输出已写入 %s", out_dir)
    return report
