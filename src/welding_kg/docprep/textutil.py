"""文本工具：内容片段拼接、LaTeX 转纯文本、清洗规则。"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple


# ---------------------------------------------------------------- 内容拼接

def concat_content(content: Optional[list], wrap_latex: bool = True) -> str:
    """把解析 JSON 的 content 片段列表拼接为单行文本。

    - text 片段原样拼接；
    - equation_inline 片段默认包装为 \\(...\\) 以保留公式信息；
    - 未知片段类型按 content 字段原样拼接。
    """
    if not content:
        return ""
    parts: List[str] = []
    for piece in content:
        if not isinstance(piece, dict):
            continue
        ptype = piece.get("type", "text")
        ptext = piece.get("content", "") or ""
        if ptype == "equation_inline":
            parts.append(f"\\({ptext}\\)" if wrap_latex else ptext)
        elif ptype == "text":
            parts.append(ptext)
        else:
            parts.append(ptext)
    return "".join(parts)


def strip_latex_math(text: str) -> str:
    """去掉 \\\\(...\\\\) 包装，返回内部 LaTeX（用于标点判断等）。"""
    return re.sub(r"\\\((.*?)\\\)", lambda m: m.group(1), text, flags=re.S)


# ---------------------------------------------------------------- LaTeX 转纯文本

_LATEX_REPLACEMENTS = [
    (re.compile(r"\\mathrm\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\mathbf\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\mathit\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\operatorname\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\text\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\times\s*"), "×"),
    (re.compile(r"\\sim\s*"), "~"),
    (re.compile(r"\\geqslant\s*"), "≥"),
    (re.compile(r"\\leqslant\s*"), "≤"),
    (re.compile(r"\\approx\s*"), "≈"),
    (re.compile(r"\\rightarrow\s*"), "→"),
    (re.compile(r"\\circ\s*"), "°"),
    (re.compile(r"\\degree\s*"), "°"),
    (re.compile(r"\\Omega\s*"), "Ω"),
    (re.compile(r"\\alpha\s*"), "α"),
    (re.compile(r"\\beta\s*"), "β"),
    (re.compile(r"\\gamma\s*"), "γ"),
    (re.compile(r"\\mu\s*"), "μ"),
    (re.compile(r"\\delta\s*"), "δ"),
    (re.compile(r"\\rho\s*"), "ρ"),
    (re.compile(r"\\eta\s*"), "η"),
    (re.compile(r"\\lambda\s*"), "λ"),
    (re.compile(r"\\theta\s*"), "θ"),
    (re.compile(r"\\pm\s*"), "±"),
    (re.compile(r"\\tag\{([^{}]*)\}"), r"（\1）"),
    (re.compile(r"_\{([^{}]*)\}"), r"\1"),        # 下标并入（如 CO_2 -> CO2）
    (re.compile(r"\{([^{}]*)\}_\{([^{}]*)\}"), r"\1\2"),
    (re.compile(r"\^\{([^{}]*)\}"), r"^\1"),      # 上标保留 ^ 记号
    (re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}"), r"\1/\2"),
    (re.compile(r"\\le\s*"), "≤"),
    (re.compile(r"\\ge\s*"), "≥"),
    (re.compile(r"\\,\s*"), " "),
    (re.compile(r"\\!\s*"), ""),
    (re.compile(r"\\[\w]+\s*"), ""),              # 其余命令去除
    (re.compile(r"[{}]"), ""),
]


def latex_to_plain(latex: str) -> str:
    """把简单 LaTeX 片段转为可读纯文本（标题/表格单元格用）。

    仅做展示性转换；原文始终保留在 raw 字段中。
    """
    text = latex.strip()
    for pattern, repl in _LATEX_REPLACEMENTS:
        text = pattern.sub(repl, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------- 字符判断

_CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def is_cjk(ch: str) -> bool:
    return bool(ch) and bool(_CJK_RE.fullmatch(ch))


_GARBLED_RE = re.compile(
    "[\ufffd\ufffc"                     # replacement characters（真乱码）
    "\x00-\x08\x0b\x0c\x0e-\x1f]"   # 控制字符
)


def has_garbled_chars(text: str) -> bool:
    """检测乱码/不可识别符号。

    注意：●○◎△□ 在本书对比表格中作为图例符号出现
    （如“注：●—非常优良；◎—优良；○—…”），不作为乱码处理。
    """
    return bool(_GARBLED_RE.search(text))


# ---------------------------------------------------------------- 清洗规则

def clean_text(text: str, kind: str, rules: dict) -> Tuple[str, List[str]]:
    """按类型执行保守的确定性清洗，返回 (清洗后文本, 已应用规则列表)。

    只做任务书 9.1 允许的格式级处理，绝不改写语义内容。
    kind: heading / paragraph / table_cell / formula / other
    """
    applied: List[str] = []
    t = text

    if kind in ("heading", "paragraph", "table_cell", "other"):
        # 1) 控制字符清理 + 行内换行合并为空格
        t = "".join(ch for ch in t if ch == "\t" or unicodedata.category(ch)[0] != "C")
        t = t.replace("\t", " ")
        if kind in ("paragraph", "table_cell", "other"):
            t = re.sub(r"\s*\n\s*", " ", t)

        # 2) 折叠连续空格
        if rules.get("collapse_spaces", True):
            prev = t
            t = re.sub(r"[ \u3000]+", " ", t)
            if t != prev:
                applied.append("collapse_spaces")

        # 3) CJK 标点两侧空格清理（仅相邻字符为 CJK 时）
        if rules.get("strip_cjk_punct_spaces", True):
            prev = t
            # 左开符号（（【“《：；，。、！？》） 前不留空格、后不留空格
            t = re.sub(r"(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\s+(?=[（【“《‘：；，。、！？）”\u3000-\u303f\uff00-\uffef])", "", t)
            t = re.sub(r"(?<=[（【“《‘：；，。、！？）])\s+(?=[\u4e00-\u9fff])", "", t)
            if t != prev:
                applied.append("strip_cjk_punct_spaces")

        # 4) 数字/单位上下文中的全角字符统一（仅局部，不影响中文标点）
        if rules.get("fullwidth_unify", True):
            prev = t
            t = re.sub(r"(?<=\d)[．](?=\d)", ".", t)
            t = re.sub(r"(?<=\d)[：](?=\d)", ":", t)
            t = re.sub(r"(?<=[0-9a-zA-Z%])[～〜](?=[0-9-])", "~", t)
            if t != prev:
                applied.append("fullwidth_unify")

        # 5) 数字之间范围符号统一（～、–、— → ~；ASCII 连字符 - 不动，避免与负号混淆）
        if rules.get("range_symbol_unify", True):
            prev = t
            t = re.sub(r"(?<=\d)\s*[～〜–—]\s*(?=\d)", "~", t)
            if t != prev:
                applied.append("range_symbol_unify")

        # 6) 行首/行尾空格
        prev = t
        t = t.strip()
        if t != prev and "strip" not in applied:
            applied.append("strip_edges")

    if kind == "heading":
        # 标题额外：去除行尾空白与多余空格，不做标点统一
        t = re.sub(r"\s+", " ", t).strip()

    if kind == "formula":
        # 公式只做边界空白清理，保留 LaTeX 全部字符
        t = t.strip()

    return t, applied


def ends_with_sentence_punct(text: str, punct_set: str) -> bool:
    """判断文本是否以句末标点结尾（忽略末尾空白与 LaTeX 包装）。"""
    stripped = strip_latex_math(text).rstrip()
    if not stripped:
        return False
    return stripped[-1] in punct_set


def normalize_caption_no(caption: str) -> Optional[str]:
    """从图题/表题提取编号，如 '表 4-23 ...' -> '4-23'，'图6-62...' -> '6-62'。

    容错处理：题注首尾可能混入印刷页码残片（如 '827 表 5-13 ...'、
    '（续）32 '、'... (GB/T 10858-2008)392 '）。
    """
    t = re.sub(r"\s+", " ", caption).strip()
    # 去掉题注开头的独立页码数字（后接空格再接 表/图）
    t = re.sub(r"^\d{2,4}\s+(?=[图表])", "", t)
    # 去掉题注末尾的独立页码数字
    t = re.sub(r"\s+\d{2,4}$", "", t)
    m = re.match(r"^[图表]\s*([0-9]+[-－—–~][0-9]+)", t)
    if m:
        return re.sub(r"[－—–~]", "-", m.group(1))
    m = re.match(r"^[图表]\s*([0-9]+)", t)
    if m:
        return m.group(1)
    return None


def clean_caption(caption: str) -> str:
    """题注规范化：去除首尾印刷页码残片、折叠空白。"""
    t = re.sub(r"\s+", " ", caption).strip()
    t = re.sub(r"^\d{2,4}\s+(?=[图表（(续)])", "", t)
    t = re.sub(r"\s+\d{2,4}$", "", t)
    return t
