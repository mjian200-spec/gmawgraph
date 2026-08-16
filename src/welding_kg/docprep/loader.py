"""输入加载：解析 JSON 与 PDF 基本信息。"""

from __future__ import annotations

import json
import os
import re
from typing import List, Optional, Tuple


def load_content_list_v2(path: str) -> List[list]:
    """加载 v2 解析 JSON：顶层为 471 页的列表，每页为原始块列表。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"解析 JSON 顶层应为 list（每页一个块列表），实际: {type(data).__name__}")
    for i, page in enumerate(data):
        if not isinstance(page, list):
            raise ValueError(f"第 {i} 页内容应为 list，实际: {type(page).__name__}")
    return data


def count_pdf_pages(pdf_path: Optional[str]) -> Tuple[Optional[int], str]:
    """统计 PDF 总页数。

    优先使用 pdfinfo（poppler）；不可用时用字节级正则统计 /Type/Page。
    返回 (页数或 None, 方法说明)。
    """
    if not pdf_path or not os.path.isfile(pdf_path):
        return None, f"pdf_missing:{pdf_path}"
    try:
        import subprocess

        out = subprocess.run(
            ["pdfinfo", pdf_path], capture_output=True, text=True, timeout=60
        )
        if out.returncode == 0:
            m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.M)
            if m:
                return int(m.group(1)), "pdfinfo"
    except Exception:
        pass
    try:
        with open(pdf_path, "rb") as f:
            data = f.read()
        count = len(re.findall(rb"/Type\s*/Page[^s]", data))
        return count, "regex_scan" if count > 0 else "regex_scan_empty"
    except Exception:
        return None, "unreadable"
