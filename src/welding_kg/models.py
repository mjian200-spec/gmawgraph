"""数据模型（Pydantic v2，规范 §8）。

未通过模型校验的数据不得传入数据库层或 LLM 层。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WeldingRequirement(BaseModel):
    """焊接需求（查询输入）。

    字段与 Case 检索字段对齐（规范 §5/§10）：process 用于硬过滤，
    其余六个结构化字段参与相似度打分；target_quality 可选，用于路径过滤。
    """

    process: str  # 焊接工艺，如 MIG / MAG / CO2焊
    material: str | None = None  # 母材材质
    thickness_mm: float | None = None  # 板厚 mm
    joint_type: str | None = None  # 接头形式
    position: str | None = None  # 焊接位置
    wire_diameter_mm: float | None = None  # 焊丝直径 mm
    shielding_gas: str | None = None  # 保护气体
    target_quality: str | None = None  # 目标质量项 code（可选，路径查询过滤用）


class CaseRecord(BaseModel):
    """历史工艺案例（cases.json 单项 + Neo4j Case 节点，规范 §5/§6）。"""

    case_id: str  # 数据库唯一键
    # ---- 检索字段 ----
    process: str
    material: str | None = None
    thickness_mm: float | None = None
    joint_type: str | None = None
    position: str | None = None
    wire_diameter_mm: float | None = None
    shielding_gas: str | None = None
    welding_current_a: float | None = None  # 焊接电流 A
    welding_voltage_v: float | None = None  # 焊接电压 V
    welding_speed_mm_s: float | None = None  # 焊接速度 mm/s
    retrieval_text: str = ""  # 用于语义检索的文本
    embedding: list[float] | None = None  # BGE-M3 向量（1024 维）
    source_refs: list[str] = Field(default_factory=list)  # 案例来源引用（如 train.xlsx 行号）
    # ---- 概念节点引用（cases.json 内为引用字符串数组）----
    conditions: list[str] = Field(default_factory=list)
    parameters: list[str] = Field(default_factory=list)
    results: list[str] = Field(default_factory=list)


class DifferenceItem(BaseModel):
    """需求与案例的单项差异（规范 §9 案例差异层）。

    差异由程序计算，不使用 LLM。数值变化为 increase/decrease/same，
    类别变化为 changed/same；code 格式为 {field}_{change}。
    """

    field: str  # 差异字段名，如 thickness_mm
    change: Literal["increase", "decrease", "same", "changed"]  # 变化类型
    code: str  # 差异代码，如 thickness_increase
    before: str  # 案例值（基准）
    after: str  # 需求值（目标）
    note: str | None = None  # 补充说明（如案例字段缺失）


class CaseMatch(BaseModel):
    """案例检索命中结果，含总分与分项得分（规范 §10）。"""

    case: CaseRecord
    total_score: float
    structured_score: float
    semantic_score: float
    field_scores: dict[str, float] = Field(default_factory=dict)  # 各结构化字段得分
    warnings: list[str] = Field(default_factory=list)


class PathNode(BaseModel):
    """推理路径上的概念节点摘要。"""

    label: str  # 节点 label
    key: str  # 唯一键值（code 或 equipment_id）
    name: str | None = None
    definition: str | None = None


class PathRelation(BaseModel):
    """推理路径上的关系摘要（规范 §5 知识关系属性）。"""

    type: str
    source_change: str | None = None  # 起点量变化方向（increase/decrease）
    target_change: str | None = None  # 终点量变化方向
    condition_text: str | None = None  # 适用条件/控制前提
    confidence: float | None = None
    provenance_type: str | None = None  # 来源强度：直接陈述/参数表推导/机理拆解/差异映射
    source_refs: list[str] = Field(default_factory=list)


class ReasoningPath(BaseModel):
    """推理路径：Condition → Parameter → Mechanism → [Mechanism] → Quality。"""

    path_id: str  # 本次运行内的稳定编号，如 p001
    difference_code: str  # 触发该路径的差异代码
    nodes: list[PathNode]
    relations: list[PathRelation]
    source_refs: list[str] = Field(default_factory=list)  # 合并去重后的证据引用
    equipment_limits: list[dict] = Field(default_factory=list)  # 附加的设备限制


class PathSelection(BaseModel):
    """LLM 路径选择结果（规范 §9 LLM 路径选择层）。"""

    selected_path_ids: list[str]  # 只允许出现在候选 path_id 中
    selection_reason: str = ""  # 简短理由，非思维链
    warnings: list[str] = Field(default_factory=list)


class ImportResult(BaseModel):
    """JSON 导入结果统计（规范 §9 导入层）。"""

    file: str  # 输入文件路径
    created: int = 0  # 新增数
    updated: int = 0  # 更新数
    skipped: int = 0  # 跳过数（内容无变化）
    errors: list[str] = Field(default_factory=list)  # 致命错误（未写入）
    warnings: list[str] = Field(default_factory=list)  # 非致命提示


class DemoResult(BaseModel):
    """端到端推理演示输出（规范 §12/§13）。"""

    case_matches: list[CaseMatch]
    base_case: CaseRecord | None  # 最高分案例
    differences: list[DifferenceItem]
    candidate_paths: list[ReasoningPath]
    selected_path_ids: list[str]
    selection_reason: str
    warnings: list[str] = Field(default_factory=list)


# ---- 外部 JSON 契约模型（规范 §6）----


class SeedNode(BaseModel):
    """graph_seed.json 节点对象。"""

    model_config = ConfigDict(populate_by_name=True)

    id: str  # 文件内稳定引用
    label: str  # 六类白名单 label
    properties: dict


class SeedRelationship(BaseModel):
    """graph_seed.json 关系对象。"""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    type: str  # 关系白名单类型
    from_: str = Field(alias="from")  # 起点节点 id
    to: str  # 终点节点 id
    properties: dict


class GraphSeedFile(BaseModel):
    """graph_seed.json 顶层契约：{nodes, relationships}。"""

    nodes: list[SeedNode]
    relationships: list[SeedRelationship]


class CasesFile(BaseModel):
    """cases.json 顶层契约：{cases}。"""

    cases: list[CaseRecord]
