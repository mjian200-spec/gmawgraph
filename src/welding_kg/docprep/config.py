"""配置加载与校验。"""

from __future__ import annotations

import copy
from typing import Any, Optional

import yaml

# 默认配置（与 preprocess_config.yaml 保持一致，缺失时兜底）
DEFAULT_CONFIG: dict = {
    "document": {
        "id": "GMAW",
        "version": "v2",
        "title": "",
        "source_pdf": "",
        "parsed_json": "",
        "parser_name": "unknown",
        "parser_version": "unknown",
        "preprocess_version": "2.1.0",
    },
    "paths": {
        "output_dir": "../data/docprep",
        "log_dir": "../logs",
        "assets_dir": "../data/docprep/assets",
    },
    "page": {
        "bounds": [0, 0, 1000, 1040],   # bbox 坐标系页面边界（由数据实测推导，见 README）
        "bbox_tolerance": 10,
        "pdf_pages_expected": None,
        "front_matter_pdf_pages": [0, 10],
        "first_body_page": 11,
        "margin_x0": 880,               # 超出此 x 视为页边标注（页眉章标题竖排）
        "column_gap": 250,              # x 中心聚类间隙阈值：超过视为分栏
    },
    "block_types": {
        "title": "heading",
        "paragraph": "paragraph",
        "table": "table",
        "image": "figure",
        "list": "list_item",
        "equation_interline": "formula",
        "page_header": "page_header",
        "page_footer": "page_footer",
        "page_number": "page_number",
        "page_footnote": "page_footnote",
        "page_aside_text": "page_aside_text",
    },
    "source_ref_codes": {
        "heading": "heading",
        "paragraph": "para",
        "table": "table",
        "table_title": "tabtitle",
        "table_note": "tabnote",
        "figure": "fig",
        "figure_title": "figtitle",
        "formula": "form",
        "list_item": "list",
        "page_header": "header",
        "page_footer": "footer",
        "page_number": "pagenum",
        "page_footnote": "footnote",
        "page_aside_text": "aside",
        "unknown": "unk",
    },
    "exclude_from_extraction": ["page_header", "page_footer", "page_number", "page_aside_text"],
    "heading": {
        # 编号层级启发式（任务书 5.4）：按顺序匹配，先匹配者生效
        "level_patterns": [
            {"regex": "^第[0-9０-９一二三四五六七八九十百]+章", "level": 1},
            {"regex": "^\\d+\\.\\d+\\.\\d+", "level": 3},
            {"regex": "^\\d+\\.\\d+", "level": 2},
            {"regex": "^\\d+\\.", "level": 4},
            {"regex": "^[（(]\\d+[）)]", "level": 5},
            {"regex": "^\\d+[）)]", "level": 6},
        ],
        "unnumbered_level1": ["前言", "序", "目录", "参考文献", "附录", "索引"],
        "appendix_pattern": "^附录\\s*[A-Za-z]",
        "appendix_level": 2,
        "front_matter_prefix": "front:",
        "level_jump_flag": True,
        "cross_line_gap": 40,           # 同页相邻无编号标题纵向间隙小于此值视为跨行标题
    },
    "text_clean": {
        "collapse_spaces": True,
        "strip_cjk_punct_spaces": True,
        "fullwidth_unify": True,
        "range_symbol_unify": True,     # 数字之间 ～–— → ~（ASCII 连字符不动）
        "strip_header_prefix": True,    # 剥离正文段落首部的页眉残留
    },
    "fusion": {
        # 参与融合的文本类型（其余内容类型是融合边界）
        "text_types": ["paragraph", "list_item", "formula", "page_footnote", "unknown"],
        # 视觉项类型（融合边界 + 内容项）
        "visual_types": ["figure", "table"],
    },
    "association": {
        "caption_gap": 60,              # 图/表与独立题注块的垂直间隙阈值
        "caption_pattern": "^[图表]\\s*\\d+[-－—–~]?\\d*",
        "min_confidence": 0.6,
    },
    "assets": {
        "pdf_crop": {
            "margin_ratio": 0.10,       # 裁剪边距比例
            "dpi": 150,
        },
        # bbox 空间 → PDF pt 经验映射（见 README）
        "x_scale": 0.7485,
        "y_scale": 1.0,
    },
    "validation": {
        # 输出中禁止出现的图谱/知识字段（任务书 v2 第 7 节）
        "forbidden_fields": [
            "entity", "entities", "relation", "relations", "ProcessWindow", "EffectRule",
            "AdjustmentRule", "process_window", "effect_rule", "adjustment_rule",
            "extraction", "knowledge", "triple",
        ],
        "source_ref_pattern": "^GMAW:v2:pdfp\\d{4}:[a-z]+\\d{2,}$",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)
    _validate(cfg, path)
    return cfg


def _validate(cfg: dict, path: str) -> None:
    doc = cfg["document"]
    if not doc.get("id") or not doc.get("version"):
        raise ValueError(f"{path}: document.id / document.version 不能为空")
    if not cfg["page"].get("bounds") or len(cfg["page"]["bounds"]) != 4:
        raise ValueError(f"{path}: page.bounds 必须为 [x0, y0, x1, y1]")
    pats = cfg["heading"]["level_patterns"]
    if not pats:
        raise ValueError(f"{path}: heading.level_patterns 不能为空")
    for p in pats:
        lv = p.get("level")
        if lv not in (1, 2, 3, 4, 5, 6):
            raise ValueError(f"{path}: 标题层级必须在 1..6 之间: {p}")
    for k in ("block_types", "source_ref_codes"):
        if not cfg.get(k):
            raise ValueError(f"{path}: {k} 不能为空")
