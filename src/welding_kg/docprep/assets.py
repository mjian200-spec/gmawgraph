"""视觉资源保留：路径解析、校验、PDF bbox 补裁（任务书 v2 第 5.7 节）。

- parsed_asset：OCR JSON 中的 image_source.path 解析为实际存在的文件
  （不复制、不修改原图）；
- pdf_crop：输入图片无效（目录路径/缺失）时，按原 PDF page+bbox 补裁
  （加边距），输出到 output/assets/pdf_crop/；
- asset_path 禁止是目录；每条资产记录含存在性、sha256 与 mime_type。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Optional, Tuple

from .model import Block

_MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png"}


def _mime_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def classify_image_path(raw_path: Optional[str], ocr_json_dir: str) -> str:
    """只读分类解析 JSON 中的图片路径：file / dir / missing / empty。"""
    if not raw_path or not str(raw_path).strip():
        return "empty"
    p = str(raw_path)
    full = p if os.path.isabs(p) else os.path.normpath(os.path.join(ocr_json_dir, p))
    if os.path.isdir(full):
        return "dir"
    if os.path.isfile(full):
        return "file"
    return "missing"


def resolve_parsed_asset(raw_path: str, ocr_json_dir: str) -> Optional[str]:
    """把解析 JSON 中的相对路径解析为绝对路径（仅当是普通文件）。"""
    if not raw_path:
        return None
    full = raw_path if os.path.isabs(raw_path) else os.path.normpath(
        os.path.join(ocr_json_dir, raw_path))
    return full if os.path.isfile(full) else None


def crop_pdf_region(pdf_path: str, pdf_page_0idx: int, bbox: list, cfg: dict,
                    out_png: str) -> bool:
    """按 bbox 从原 PDF 裁剪区域（加边距）保存为 PNG。返回是否成功。"""
    x_scale = cfg["assets"]["x_scale"]
    y_scale = cfg["assets"]["y_scale"]
    dpi = cfg["assets"]["pdf_crop"]["dpi"]
    margin = cfg["assets"]["pdf_crop"]["margin_ratio"]
    x0 = bbox[0] * x_scale
    x1 = bbox[2] * x_scale
    y0 = bbox[1] * y_scale
    y1 = bbox[3] * y_scale
    w = max(x1 - x0, 40)
    h = max(y1 - y0, 24)
    px = dpi / 72.0
    pad_x = w * margin
    pad_y = h * margin
    try:
        subprocess.run(
            ["pdftoppm", "-f", str(pdf_page_0idx + 1), "-l", str(pdf_page_0idx + 1),
             "-r", str(dpi),
             "-x", str(max(0, int((x0 - pad_x) * px))),
             "-y", str(max(0, int((y0 - pad_y) * px))),
             "-W", str(int((w + 2 * pad_x) * px)),
             "-H", str(int((h + 2 * pad_y) * px)),
             "-png", pdf_path, out_png.rsplit(".", 1)[0]],
            capture_output=True, check=True, timeout=60)
        produced = f"{out_png.rsplit('.', 1)[0]}-{pdf_page_0idx + 1:03d}.png"
        if os.path.isfile(produced) and produced != out_png:
            os.replace(produced, out_png)
        return os.path.isfile(out_png)
    except Exception:
        return False


def build_asset_record(block: Block, cfg: dict, ocr_json_dir: str, pdf_path: str,
                       assets_dir: str, project_dir: str) -> Tuple[dict, bool]:
    """为 figure/table 块构建资产记录，必要时生成 pdf_crop。

    返回 (资产记录 dict, 是否 pdf_crop)。
    """
    raw_path = block.extra.get("image_path") or ""
    original = raw_path if raw_path else None
    resolved = resolve_parsed_asset(raw_path, ocr_json_dir)
    if resolved:
        rel = os.path.relpath(resolved, project_dir)
        return {
            "asset_path": rel,
            "original_asset_path": original,
            "asset_origin": "parsed_asset",
            "asset_exists": True,
            "sha256": _sha256(resolved),
            "mime_type": _mime_for(resolved),
        }, False

    # 输入图片无效（目录路径/缺失/空）→ 按原 PDF page+bbox 补裁
    crop_dir = os.path.join(assets_dir, "pdf_crop")
    os.makedirs(crop_dir, exist_ok=True)
    out_png = os.path.join(crop_dir, f"{block.block_id.replace(':', '_')}.png")
    if not os.path.isfile(out_png):
        ok = crop_pdf_region(pdf_path, block.pdf_page, block.bbox, cfg, out_png)
        if not ok:
            return {
                "asset_path": "",
                "original_asset_path": original,
                "asset_origin": "pdf_crop",
                "asset_exists": False,
                "sha256": "",
                "mime_type": "",
            }, True
    rel = os.path.relpath(out_png, project_dir)
    return {
        "asset_path": rel,
        "original_asset_path": original,
        "asset_origin": "pdf_crop",
        "asset_exists": True,
        "sha256": _sha256(out_png),
        "mime_type": "image/png",
    }, True
