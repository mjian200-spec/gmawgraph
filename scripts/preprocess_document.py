#!/usr/bin/env python3
"""GMAW 文档结构化预处理命令行入口（任务书 v2）。

用法：
    python preprocess_document.py audit    --config preprocess_config.yaml
    python preprocess_document.py run      --config preprocess_config.yaml
    python preprocess_document.py validate --config preprocess_config.yaml

- audit:    只读审计（统计 + 资源分类），不生成任何补裁资源。
- run:      完整结构化预处理 → document_structure.json（主输出）等。
- validate: 对**当前输出**执行结构校验 + 资源校验，并与两次全新重跑
            逐文件哈希比对（确定性验收）。
"""

from __future__ import annotations

import argparse
import hashlib
import json as _json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from welding_kg.docprep import PREPROCESS_VERSION  # noqa: E402
from welding_kg.docprep.config import load_config  # noqa: E402
from welding_kg.docprep.pipeline import PipelineError, run_audit, run_pipeline  # noqa: E402
from welding_kg.docprep.model import Block, SectionNode  # noqa: E402
from welding_kg.docprep.validate import check_raw_preserved, validate_outputs  # noqa: E402
from welding_kg.docprep.loader import load_content_list_v2  # noqa: E402
from welding_kg.docprep.structure import build_content_sequence  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    fh = logging.FileHandler(os.path.join(log_dir, "preprocess.log"),
                             encoding="utf-8", mode="w")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def _config_path(args) -> str:
    path = args.config or os.path.join(ROOT, "config", "docprep.yaml")
    if not os.path.isfile(path):
        raise SystemExit(f"配置文件不存在: {path}")
    return path


def _base_dir(config_path: str) -> str:
    # 相对路径基于配置所在目录解析（任务书要求单一命令可执行，路径可预期）
    return os.path.dirname(os.path.abspath(config_path))


def cmd_audit(args) -> int:
    cfg = load_config(_config_path(args))
    _setup_logging(os.path.join(_base_dir(_config_path(args)), cfg["paths"]["log_dir"]))
    log = logging.getLogger("gmaw_preprocess")
    log.info("审计模式（只读，不修改任何输入文件、不生成补裁资源）")
    try:
        run_audit(cfg, _base_dir(_config_path(args)), write=True)
    except PipelineError as e:
        log.error("审计失败: %s", e)
        return 2
    return 0


def cmd_run(args) -> int:
    config_path = _config_path(args)
    cfg = load_config(config_path)
    base = _base_dir(config_path)
    _setup_logging(os.path.join(base, cfg["paths"]["log_dir"]))
    log = logging.getLogger("gmaw_preprocess")
    log.info("完整预处理模式，preprocess_version=%s", PREPROCESS_VERSION)
    try:
        run_pipeline(cfg, base)
    except PipelineError as e:
        log.error("预处理失败（最终输出未发布）: %s", e)
        return 2
    except Exception:
        log.exception("预处理异常中止（最终输出未发布）")
        return 2
    return 0


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_jsonl(p):
    with open(p, encoding="utf-8") as f:
        return [_json.loads(l) for l in f if l.strip()]


def _check_current_outputs(cfg: dict, base: str, out_dir: str, log) -> int:
    """对当前输出执行结构校验 + 资源校验（validate 的对象是当前产物）。"""
    log.info("对当前输出执行结构校验（validate_outputs）")
    try:
        all_blocks = _load_jsonl(os.path.join(out_dir, "normalized_blocks.jsonl"))
        blocks = [Block.from_json_obj(b) for b in all_blocks]
        blocks.sort(key=lambda b: (b.pdf_page, b.block_order))
        struct = _json.load(open(os.path.join(out_dir, "document_structure.json"),
                                 encoding="utf-8"))
        sections = [SectionNode.from_json_obj(s) for s in struct["sections"]]
        pages_raw = load_content_list_v2(os.path.join(base, cfg["document"]["parsed_json"]))
        # 重建内容序列（排除版边块）
        pages_blocks: dict = {}
        for b in blocks:
            pages_blocks.setdefault(b.pdf_page, []).append(b)
        pages_sorted = [pages_blocks[p] for p in sorted(pages_blocks)]
        seq, _ = build_content_sequence(pages_sorted, cfg)
        raw_violations = check_raw_preserved(blocks, pages_raw)
        quality = validate_outputs(blocks, sections, seq, cfg, raw_violations)
        cur_fails = [c["check"] for c in quality["checks"]
                     if not c["ok"] and c.get("level") == "error"]
        if cur_fails:
            log.error("当前输出结构校验失败: %s", cur_fails)
            return 2
        # 资源校验：figure/table 项的 asset_path 存在、是普通文件、可读、哈希一致
        bad_assets = 0
        n_checked = 0
        for s in sections:
            for item in s.content:
                if item.item_type not in ("figure", "table"):
                    continue
                n_checked += 1
                p = os.path.join(os.path.dirname(base), item.asset_path) \
                    if not os.path.isabs(item.asset_path) else item.asset_path
                if not os.path.isfile(p):
                    bad_assets += 1
                    log.error("资源缺失: %s (%s)", item.asset_path, item.item_id)
                    continue
                try:
                    with open(p, "rb") as f:
                        f.read(1)
                    actual_hash = _hash_file(p)
                except OSError:
                    bad_assets += 1
                    log.error("资源不可读: %s", item.asset_path)
                    continue
                if item.sha256 and actual_hash != item.sha256:
                    bad_assets += 1
                    log.error("资源哈希不一致: %s", item.asset_path)
        if bad_assets:
            log.error("资源校验失败: %d 处", bad_assets)
            return 2
        log.info("资源校验通过: %d 个视觉项", n_checked)
        pending = [c["check"] for c in quality["checks"]
                   if not c["ok"] and c.get("level") in ("warning", "info")]
        log.info("当前输出结构校验通过；提示项: %s", pending or "无")
    except Exception as e:
        log.exception("当前输出校验异常: %s", e)
        return 2
    return 0


def cmd_validate(args) -> int:
    config_path = _config_path(args)
    cfg = load_config(config_path)
    base = _base_dir(config_path)
    _setup_logging(os.path.join(base, cfg["paths"]["log_dir"]))
    log = logging.getLogger("gmaw_preprocess")

    out_dir = os.path.join(base, cfg["paths"]["output_dir"])
    required = ["document_structure.json", "source_registry.json",
                "normalized_blocks.jsonl", "preprocessing_report.json"]
    missing = [f for f in required if not os.path.isfile(os.path.join(out_dir, f))]
    if missing:
        log.error("缺少输出文件，请先运行 run: %s", missing)
        return 2

    # 1) 当前输出：结构 + 资源校验
    rc = _check_current_outputs(cfg, base, out_dir, log)
    if rc != 0:
        return rc

    # 2) 重复运行一致性：临时目录两次全新运行哈希一致，且与当前输出一致
    log.info("重复运行一致性检查（临时目录运行 2 次，并与当前输出比对）")
    hashes = []
    try:
        for i in (1, 2):
            tmp = tempfile.mkdtemp(prefix="gmaw_prep_det_")
            cfg2 = dict(cfg)
            cfg2["paths"] = dict(cfg["paths"])
            cfg2["paths"]["output_dir"] = tmp
            cfg2["paths"]["log_dir"] = os.path.join(tmp, "logs")
            run_pipeline(cfg2, base)
            sig = {}
            for fname in ("document_structure.json", "source_registry.json",
                          "normalized_blocks.jsonl", "preprocessing_report.json"):
                sig[fname] = _hash_file(os.path.join(tmp, fname))
            hashes.append(sig)
    except Exception as e:
        log.exception("确定性检查运行失败: %s", e)
        return 2

    identical = hashes[0] == hashes[1]
    log.info("两次运行输出哈希一致: %s", identical)
    if not identical:
        for fname in hashes[0]:
            if hashes[0][fname] != hashes[1][fname]:
                log.error("不一致文件: %s", fname)
        return 2

    # 3) 当前输出与全新重跑比对（检测被修改/过期/不同运行混出的输出）
    current_mismatch = []
    for fname in hashes[0]:
        cur_path = os.path.join(out_dir, fname)
        if not os.path.isfile(cur_path):
            current_mismatch.append(f"{fname}（当前输出缺失）")
            continue
        if _hash_file(cur_path) != hashes[0][fname]:
            current_mismatch.append(f"{fname}（与全新重跑不一致：当前输出被修改或过期）")
    if current_mismatch:
        log.error("当前 output/ 与全新重跑结果不一致: %s", current_mismatch)
        return 2
    log.info("当前 output/ 与全新重跑结果一致 ✓")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="preprocess_document.py",
        description=f"GMAW 文档结构化预处理 v{PREPROCESS_VERSION}"
                    f"（任务书 v2，welding_kg.docprep 模块，conda 环境 GMAWGraph）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_audit = sub.add_parser("audit", help="只读审计模式")
    p_audit.add_argument("--config", help="配置文件路径（默认脚本目录 preprocess_config.yaml）")
    p_audit.set_defaults(func=cmd_audit)
    p_run = sub.add_parser("run", help="完整结构化预处理模式")
    p_run.add_argument("--config", help="配置文件路径")
    p_run.set_defaults(func=cmd_run)
    p_val = sub.add_parser("validate", help="当前输出校验 + 资源校验 + 重复运行一致性")
    p_val.add_argument("--config", help="配置文件路径")
    p_val.set_defaults(func=cmd_validate)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
