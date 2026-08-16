#!/usr/bin/env python3
"""welding_kg.docprep 独立资源完整性验证脚本（任务书 v2 第 9.7 节）。

对 data/docprep/document_structure.json 中全部 figure/table 项检查：
- asset_path 存在、是普通文件、可读取、哈希可计算且与记录一致；
- figure/table 数量与输入一致（741 / 193）；
- 33 个 pdf_crop 表格图片；
- 输入中的有效图片未被修改（parsed_asset 的哈希与输入目录文件一致）。

用法（GMAWGraph conda 环境）：
    /ENV/Anaconda/envs/jm/GMAWGraph/bin/python tests/check_docprep_assets.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    struct_path = os.path.join(ROOT, "data", "docprep", "document_structure.json")
    if not os.path.isfile(struct_path):
        print(f"缺少 {struct_path}，请先运行 scripts/preprocess_document.py run")
        return 2
    struct = json.load(open(struct_path, encoding="utf-8"))

    items = [i for s in struct["sections"] for i in s["content"]
             if i["item_type"] in ("figure", "table")]
    figs = [i for i in items if i["item_type"] == "figure"]
    tabs = [i for i in items if i["item_type"] == "table"]

    n_ok = 0
    n_bad = 0
    problems = []

    def report(cond, msg):
        nonlocal n_ok, n_bad
        if cond:
            n_ok += 1
        else:
            n_bad += 1
            problems.append(msg)

    report(len(figs) == 741, f"figure 数量 {len(figs)} != 741")
    report(len(tabs) == 193, f"table 数量 {len(tabs)} != 193")
    report(sum(1 for i in tabs if i["asset_origin"] == "pdf_crop") == 33,
           f"pdf_crop 表格 {sum(1 for i in tabs if i['asset_origin'] == 'pdf_crop')} != 33")

    for i in items:
        p = i["asset_path"]
        full = p if os.path.isabs(p) else os.path.join(ROOT, p)
        label = f"{i['item_id']} {i['asset_path']}"
        if os.path.isdir(full):
            report(False, f"{label}: 是目录")
            continue
        if not os.path.isfile(full):
            report(False, f"{label}: 文件不存在")
            continue
        try:
            with open(full, "rb") as f:
                f.read(1)
        except OSError as e:
            report(False, f"{label}: 不可读 ({e})")
            continue
        try:
            h = sha256(full)
        except OSError as e:
            report(False, f"{label}: 哈希计算失败 ({e})")
            continue
        if i.get("sha256") and h != i["sha256"]:
            report(False, f"{label}: 哈希不一致")
        else:
            report(True, "")

    # parsed_asset 的原图不得被修改：哈希与输入目录中文件一致
    for i in items:
        if i["asset_origin"] != "parsed_asset":
            continue
        full = os.path.join(ROOT, i["asset_path"])
        src = os.path.join(ROOT, "..", "GMAW", "hybrid_ocr",
                           i["original_asset_path"] or "")
        src = os.path.normpath(src)
        if os.path.isfile(src) and sha256(full) == sha256(src):
            report(True, "")
        else:
            report(False, f"{i['item_id']}: parsed_asset 与输入原图不一致")

    print(f"资源完整性检查: {n_ok} 通过, {n_bad} 失败")
    for p in problems[:20]:
        print(f"  ✗ {p}")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
