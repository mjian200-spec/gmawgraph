"""LLM 路径选择层（规范 §9 LLM 路径选择层）。

Qwen3-32B 只允许从候选路径中选择 path_id 并给出简短理由；
结果必须通过 Pydantic 校验，不存在的 path_id 直接拒绝；
不得输出思维链，也不得生成图中不存在的路径。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from openai import OpenAI

import httpx2  # openai 3.x 的底层 HTTP 客户端库
from pydantic import ValidationError

from .models import (
    CaseRecord,
    DifferenceItem,
    PathSelection,
    ReasoningPath,
    WeldingRequirement,
)
from .settings import Settings

# 选择结果 JSON 契约（供 prompt 与校验共用）
_SELECTION_SCHEMA = '{"selected_path_ids": ["p000", "p001"], "selection_reason": "简短理由"}'


@lru_cache(maxsize=8)
def _llm_client(base_url: str, api_key: str) -> OpenAI:
    """按 (base_url, api_key) 缓存 OpenAI 兼容客户端实例。

    trust_env=False：本机服务不走系统代理（环境变量 ALL_PROXY 等）。
    """
    http_client = httpx2.Client(trust_env=False, timeout=300.0)
    return OpenAI(base_url=base_url, api_key=api_key or "EMPTY", http_client=http_client)


def _format_requirement(requirement: WeldingRequirement) -> str:
    """把需求格式化为提示词中的单行描述。"""
    parts = [f"工艺={requirement.process}"]
    for field in ("material", "thickness_mm", "joint_type", "position",
                  "wire_diameter_mm", "shielding_gas", "target_quality"):
        value = getattr(requirement, field)
        if value is not None:
            parts.append(f"{field}={value}")
    return "，".join(parts)


def _format_path(path: ReasoningPath) -> str:
    """把候选路径格式化为提示词中的可读文本行。

    只提供节点链与设备限制等推理所需信息；证据溯源（source_refs）
    仅供人工复核，不进入 LLM 上下文。
    """
    chain = " → ".join(
        f"{n.label}:{n.name or n.key}" for n in path.nodes
    )
    limits = (
        f"；设备限制：{'，'.join(l['equipment_id'] for l in path.equipment_limits)}"
        if path.equipment_limits
        else ""
    )
    return f"[{path.path_id}] {chain}{limits}"


def _build_prompt(
    requirement: WeldingRequirement,
    base_case: CaseRecord,
    differences: list[DifferenceItem],
    candidates: list[ReasoningPath],
) -> str:
    """构造路径选择的提示词（只选不造，禁止思维链）。"""
    diff_lines = "\n".join(
        f"- {d.code}：案例值 {d.before} → 需求值 {d.after}（{d.change}）"
        for d in differences
    )
    candidate_lines = "\n".join(_format_path(p) for p in candidates)
    return (
        "你是焊接工艺推理助手。给定焊接需求、基准案例、需求与案例的差异，"
        "以及从知识图谱中查询出的候选推理路径，请从候选中选择最有助于"
        "解释该差异对焊接质量影响的 1-3 条路径。\n\n"
        f"焊接需求：{_format_requirement(requirement)}\n"
        f"基准案例：{base_case.case_id}（{base_case.retrieval_text or base_case.process}）\n"
        f"差异列表：\n{diff_lines}\n\n"
        f"候选路径（只能从中选择，不得编造）：\n{candidate_lines}\n\n"
        f"要求：\n"
        f"1. 只输出一个 JSON 对象：{_SELECTION_SCHEMA}\n"
        f"2. selected_path_ids 只能包含上述候选路径编号；若都不合适，返回空数组\n"
        f"3. selection_reason 用一句话说明选择理由\n"
        f"4. 禁止输出思维链、分析过程或 JSON 之外的任何内容"
    )


def _parse_selection(text: str) -> dict | None:
    """稳健解析 LLM 返回的 JSON（去代码围栏、正则提取首个 JSON 对象）。"""
    cleaned = text.strip()
    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 兜底：取第一个 {...} 块
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def filter_valid_ids(
    selection: PathSelection, candidates: list[ReasoningPath]
) -> PathSelection:
    """过滤掉不在候选中的 path_id（规范 §9：不存在的 path_id 直接拒绝）。

    抽成纯函数便于冒烟测试：返回过滤后的选择结果并附拒绝 warning。
    """
    valid_ids = {p.path_id for p in candidates}
    kept = [pid for pid in selection.selected_path_ids if pid in valid_ids]
    rejected = [pid for pid in selection.selected_path_ids if pid not in valid_ids]
    if rejected:
        selection.warnings.append(f"已拒绝不在候选中的 path_id：{rejected}")
    selection.selected_path_ids = kept
    return selection


def select_reasoning_paths(
    requirement: WeldingRequirement,
    base_case: CaseRecord,
    differences: list[DifferenceItem],
    candidates: list[ReasoningPath],
) -> PathSelection:
    """调用 LLM 从候选路径中选择最终路径（规范 §9）。

    返回只包含候选内合法 path_id 的 PathSelection；
    调用失败或全部无效时返回空选择并附 warning。
    """
    settings = Settings.from_env()

    # 无候选或差异为空时无需调用 LLM
    if not candidates or not differences:
        return PathSelection(
            selected_path_ids=[],
            selection_reason="",
            warnings=["无候选路径或差异列表为空，跳过 LLM 选择"],
        )

    prompt = _build_prompt(requirement, base_case, differences, candidates)
    try:
        client = _llm_client(settings.llm_base_url, settings.llm_api_key)
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=512,
            response_format={"type": "json_object"},
            # 关闭 Qwen3 思考链，禁止输出思维过程（规范 §9）
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 LLM 不可用时不阻断流程
        return PathSelection(
            selected_path_ids=[],
            selection_reason="",
            warnings=[f"LLM 调用失败：{exc}"],
        )

    parsed = _parse_selection(content)
    if parsed is None:
        return PathSelection(
            selected_path_ids=[],
            selection_reason="",
            warnings=[f"LLM 返回内容无法解析为 JSON：{content[:200]}"],
        )

    # Pydantic 校验（规范 §8：未通过校验的数据不得进入下游）
    try:
        selection = PathSelection.model_validate(parsed)
    except ValidationError as exc:
        return PathSelection(
            selected_path_ids=[],
            selection_reason="",
            warnings=[f"LLM 返回不符合约定结构：{exc}"],
        )

    # 不存在的 path_id 直接拒绝（规范 §9）
    return filter_valid_ids(selection, candidates)
