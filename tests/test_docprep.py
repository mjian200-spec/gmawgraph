"""welding_kg.docprep 回归测试（任务书 v2，pytest，无外部服务）。

覆盖任务书 v2 第 8 节全部 17 条验收标准，以及历史 bug 回归：
- 章节 end 范围 bug（曾因对象 id 查询 block_id 键映射导致全部章节
  结束于最后一页）；
- 标题/正文粘连拆分（标题字段不得包含正文）。

运行（GMAWGraph conda 环境）：
    /ENV/Anaconda/envs/jm/GMAWGraph/bin/python -m pytest -q tests/test_docprep.py
或独立运行：
    /ENV/Anaconda/envs/jm/GMAWGraph/bin/python tests/test_docprep.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from welding_kg.docprep.config import load_config  # noqa: E402
from welding_kg.docprep.pipeline import run_pipeline  # noqa: E402
from welding_kg.docprep.model import Block, SectionNode  # noqa: E402
from welding_kg.docprep.loader import load_content_list_v2  # noqa: E402
from welding_kg.docprep.structure import build_content_sequence  # noqa: E402
from welding_kg.docprep.validate import (check_raw_preserved, validate_outputs)  # noqa: E402

CFG_PATH = os.path.join(ROOT, "config", "docprep.yaml")
CFG_DIR = os.path.dirname(CFG_PATH)   # 相对路径解析基准


def _load(out_dir, fname):
    path = os.path.join(out_dir, fname)
    if fname.endswith(".jsonl"):
        return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    return json.load(open(path, encoding="utf-8"))


def _hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="module")
def cfg():
    return load_config(CFG_PATH)


@pytest.fixture(scope="module")
def out_dir(cfg):
    """在临时目录运行一次完整流水线，全部测试共享。"""
    tmp = tempfile.mkdtemp(prefix="gmaw_docprep_test_")
    cfg2 = dict(cfg)
    cfg2["paths"] = dict(cfg["paths"])
    cfg2["paths"]["output_dir"] = tmp
    cfg2["paths"]["log_dir"] = os.path.join(tmp, "logs")
    run_pipeline(cfg2, CFG_DIR)
    return tmp


@pytest.fixture(scope="module")
def current(cfg, out_dir):
    blocks = [Block.from_json_obj(b)
              for b in _load(out_dir, "normalized_blocks.jsonl")]
    blocks.sort(key=lambda b: (b.pdf_page, b.block_order))
    struct = _load(out_dir, "document_structure.json")
    sections = [SectionNode.from_json_obj(s) for s in struct["sections"]]
    pages = {}
    for b in blocks:
        pages.setdefault(b.pdf_page, []).append(b)
    pages_sorted = [pages[p] for p in sorted(pages)]
    seq, _ = build_content_sequence(pages_sorted, cfg)
    return blocks, sections, seq


# ---------------------------------------------------------------- 17 条验收标准

def test_acceptance_criteria(cfg, out_dir, current):
    blocks, sections, seq = current
    pages_raw = load_content_list_v2(
        os.path.join(CFG_DIR, cfg["document"]["parsed_json"]))
    raw_violations = check_raw_preserved(blocks, pages_raw)
    quality = validate_outputs(blocks, sections, seq, cfg, raw_violations)
    assert quality["fail_count"] == 0, \
        [c["check"] for c in quality["checks"] if not c["ok"]]
    bool_items = {k: v for k, v in quality["acceptance"].items()
                  if isinstance(v, bool)}
    assert all(bool_items.values()), \
        {k: v for k, v in bool_items.items() if not v}
    assert quality["acceptance"]["16_deterministic"] == "verified_by_validate_command"
    report = _load(out_dir, "preprocessing_report.json")
    assert report["quality"]["fail_count"] == quality["fail_count"]


# ---------------------------------------------------------------- 历史 bug 回归

def test_section_range_regression(out_dir):
    """章节 end 不得全部结束于最后一页（曾因 id() 查询 block_id 键映射）。"""
    struct = _load(out_dir, "document_structure.json")
    sections = [s for s in struct["sections"] if s["level"] > 0]
    last_page = max(s["start_page"] or 0 for s in sections)
    end_last = [s for s in sections if s["end_page"] == last_page]
    assert len(end_last) / len(sections) < 0.05, f"{len(end_last)}/{len(sections)}"
    s = next(x for x in sections if x["section_id"] == "gmaw:v2:sec:2.1.1")
    s2 = next(x for x in sections if x["section_id"] == "gmaw:v2:sec:2.1.2")
    assert s["end_page"] <= s2["start_page"], f"{s['end_page']} vs {s2['start_page']}"


def test_split_title_regression(out_dir):
    """标题与正文粘连拆分后，标题字段不得包含正文；不可拆则 needs_review。"""
    struct = _load(out_dir, "document_structure.json")
    s = next((x for x in struct["sections"]
              if "简单熔入型" in x["normalized_title"]), None)
    assert s is not None
    assert len(s["normalized_title"]) < 40, s["normalized_title"]
    blocks = _load(out_dir, "normalized_blocks.jsonl")
    glued = [b for b in blocks if "简单熔入型" in b.get("normalized_text", "")
             and b["block_type"] == "heading"]
    assert glued and glued[0]["status"] == "needs_review", \
        str(glued[0]["status"] if glued else None)
    report = _load(out_dir, "preprocessing_report.json")
    assert report["stages"]["text_clean"]["summary"]["heading_body_boundary_suspects"] >= 1
    bad_titles = [x["section_id"] for x in struct["sections"]
                  if re.search(r"[。！？；]", x["normalized_title"])]
    assert not bad_titles, bad_titles[:5]
    long_titles = [x["section_id"] for x in struct["sections"]
                   if len(x["normalized_title"]) > 50]
    assert not long_titles, long_titles[:5]


# ---------------------------------------------------------------- 结构不变量

def test_structure_invariants(out_dir):
    struct = _load(out_dir, "document_structure.json")
    assert struct["section_order"][0] == "gmaw:v2:sec:root"
    sections = struct["sections"]
    assert len(sections) == 427, len(sections)
    assert struct["section_order"] == [s["section_id"] for s in sections]
    report = _load(out_dir, "preprocessing_report.json")
    ss = report["stages"]["structure"]["summary"]
    assert 700 <= ss["text_segments"] <= 900, ss["text_segments"]
    assert ss["figures"] == 741
    assert ss["tables"] == 193
    assert ss["subsections"] == len(sections) - 1
    assets = report["stages"]["assets"]["summary"]
    assert assets["pdf_crop"] == 33, assets
    assert assets["parsed_asset"] == 901, assets
    root = next(x for x in sections if x["section_id"] == "gmaw:v2:sec:root")
    top_level = [x for x in sections if x["level"] == 1]
    sub_refs = [i for i in root["content"] if i["item_type"] == "subsection"]
    assert {i["section_id"] for i in sub_refs} == {x["section_id"] for x in top_level}


def test_fusion_boundaries(out_dir):
    struct = _load(out_dir, "document_structure.json")
    s = next(x for x in struct["sections"] if x["section_id"] == "gmaw:v2:sec:2.1.1")
    types = [i["item_type"] for i in s["content"]]
    assert not any(types[i] == types[i + 1] == "text_segment"
                   for i in range(len(types) - 1)), types
    blocks = _load(out_dir, "normalized_blocks.jsonl")
    block_by_id = {b["block_id"]: b for b in blocks}
    for sec in struct["sections"]:
        for item in sec["content"]:
            if item["item_type"] != "text_segment":
                continue
            assert all(m in block_by_id for m in item["member_block_ids"]), item["item_id"]
            if item["sources"]:
                assert len(item["sources"]) == len(item["member_block_ids"])


def test_caption_non_duplication(out_dir):
    struct = _load(out_dir, "document_structure.json")
    caps = [i["caption"] for s in struct["sections"] for i in s["content"]
            if i["item_type"] in ("figure", "table") and i.get("caption")]
    assert len(caps) >= 700, len(caps)
    dup = 0
    for s in struct["sections"]:
        for item in s["content"]:
            if item["item_type"] != "text_segment":
                continue
            text = item["text"].strip()
            if (re.match(r"^[图表]\s*\d+[-－—–~]?\d*\s", text)
                    and not re.search(r"[。！？]", text)
                    and "、" not in text[:20] and len(text) < 60):
                dup += 1
    assert dup == 0, dup


def test_assets(out_dir):
    struct = _load(out_dir, "document_structure.json")
    figs = [i for s in struct["sections"] for i in s["content"] if i["item_type"] == "figure"]
    tabs = [i for s in struct["sections"] for i in s["content"] if i["item_type"] == "table"]
    assert len(figs) == 741, len(figs)
    assert len(tabs) == 193, len(tabs)
    assert all(i["asset_exists"] for i in figs)
    assert all(i["asset_exists"] for i in tabs)
    assert sum(1 for i in tabs if i["asset_origin"] == "pdf_crop") == 33
    assert all(not os.path.isdir(os.path.join(ROOT, i["asset_path"])) for i in figs + tabs)
    assert all(os.path.isfile(os.path.join(ROOT, i["asset_path"])) for i in figs + tabs)
    assert all(len(i["sha256"]) == 64 for i in figs + tabs)
    reg = _load(out_dir, "source_registry.json")["registry"]
    for i in figs + tabs:
        entry = reg[i["source_ref"]]
        assert entry["pdf_page"] == i["pdf_page"]
        assert entry["bbox"] == i["bbox"]
        assert entry["asset_path"] == i["asset_path"]


def test_raw_preserved(cfg, out_dir):
    blocks = [Block.from_json_obj(b) for b in _load(out_dir, "normalized_blocks.jsonl")]
    pages_raw = load_content_list_v2(
        os.path.join(CFG_DIR, cfg["document"]["parsed_json"]))
    violations = check_raw_preserved(blocks, pages_raw)
    assert not violations, violations[:5]


def test_no_knowledge_fields(out_dir):
    struct = _load(out_dir, "document_structure.json")
    forbidden = re.compile(r"entity|relation|process_?window|effect_?rule|adjustment_?rule",
                           re.IGNORECASE)

    def scan(obj):
        hits = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if forbidden.search(str(k)):
                    hits.append(k)
                hits.extend(scan(v))
        elif isinstance(obj, list):
            for v in obj:
                hits.extend(scan(v))
        return hits

    assert not scan(struct), scan(struct)[:5]


def test_static_guards(out_dir):
    import welding_kg.docprep as pkg

    src = open(os.path.join(ROOT, "src", "welding_kg", "docprep", "validate.py"),
               encoding="utf-8").read()
    acc_start = src.index("acceptance = {")
    acc_end = src.index("}", acc_start) + 1
    acc_body = src[acc_start:acc_end]
    literals = [m for m in re.finditer(r"(?<![\"'\w_])(True|False)(?![\w_])", acc_body)]
    assert not literals, [m.group(1) for m in literals]

    report = _load(out_dir, "preprocessing_report.json")
    assert report["meta"]["preprocess_version"] == pkg.PREPROCESS_VERSION
    expected_keys = {
        "section_id_unique", "section_tree_no_orphans", "section_tree_no_cycles",
        "section_ranges_correct", "sections_not_all_end_at_last_page",
        "blocks_bound_to_deepest_section", "content_order_strict",
        "traversal_matches_reading_order", "fusion_no_cross_boundary",
        "fusion_restores_all_text_blocks", "all_figures_have_assets",
        "all_tables_have_assets", "asset_path_not_directory", "pdf_crop_count_33",
        "visual_source_ref_resolvable", "captions_not_duplicated_in_content",
        "raw_text_preserved", "no_knowledge_fields",
        "deterministic_across_runs", "no_human_gate",
    }
    actual_keys = {c["key"] for c in report["quality"]["checks"]}
    assert actual_keys == expected_keys, actual_keys ^ expected_keys


def test_determinism(cfg):
    sigs = []
    for _ in (1, 2):
        tmp = tempfile.mkdtemp(prefix="gmaw_docprep_det_")
        cfg2 = dict(cfg)
        cfg2["paths"] = dict(cfg["paths"])
        cfg2["paths"]["output_dir"] = tmp
        cfg2["paths"]["log_dir"] = os.path.join(tmp, "logs")
        run_pipeline(cfg2, CFG_DIR)
        sig = {}
        for fname in ("document_structure.json", "source_registry.json",
                      "normalized_blocks.jsonl", "preprocessing_report.json"):
            sig[fname] = _hash_file(os.path.join(tmp, fname))
        sigs.append(sig)
    assert sigs[0] == sigs[1]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
