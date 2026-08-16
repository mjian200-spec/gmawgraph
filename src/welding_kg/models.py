"""数据模型（Pydantic v2，规范 §8）。

未通过模型校验的数据不得传入数据库层或 LLM 层。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 本阶段支持的修正参数白名单（adjustment_generation_spec §1）：
# 修正量生成只处理这三个参数；图谱中出现其他带步长参数时安全忽略。
SUPPORTED_PARAMETER_CODES = ("welding_current", "arc_voltage", "welding_speed")


class WeldingRequirement(BaseModel):
    """焊接需求（查询输入）。

    字段与 Case 检索字段对齐（规范 §5/§10）：process 用于硬过滤，
    其余六个结构化字段参与相似度打分；target_quality 可选，用于路径过滤。
    requirement_id / control_mode 为修正量生成阶段的扩展输入
    （adjustment_generation_spec §15 CLI 输出、§10 控制模式检查）。
    """

    process: str  # 焊接工艺，如 MIG / MAG / CO2焊
    material: str | None = None  # 母材材质
    thickness_mm: float | None = None  # 板厚 mm
    joint_type: str | None = None  # 接头形式
    position: str | None = None  # 焊接位置
    wire_diameter_mm: float | None = None  # 焊丝直径 mm
    shielding_gas: str | None = None  # 保护气体
    target_quality: str | None = None  # 目标质量项 code（可选，路径查询过滤用）
    requirement_id: str | None = None  # 需求标识（修正量 CLI 输出用，可选）
    control_mode: Literal["unified", "separate"] | None = None  # 设备控制模式：一元/分别


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
    """案例检索命中结果，含总分与分项得分（规范 §10）。

    各分项得分均归一化在 [0, 1]：结构化得分按权重归一化，语义得分为
    裁剪到 [0, 1] 的余弦相似度，总分为两者凸组合。
    """

    case: CaseRecord
    total_score: float = Field(ge=0.0, le=1.0)
    structured_score: float = Field(ge=0.0, le=1.0)
    semantic_score: float = Field(ge=0.0, le=1.0)
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


# ---- 修正量生成模型（adjustment_generation_spec §5/§6）----


class EquipmentStep(BaseModel):
    """设备对某参数的调整步长，来自图谱 Equipment-LIMITS→Parameter 关系。

    取值优先级（规范 §4）：adjustment_step（manual）→ default_step
    （project_default）→ 均不存在时停止生成该参数并返回告警。
    """

    model_config = ConfigDict(validate_assignment=True)

    parameter_code: str
    step: float = Field(gt=0)  # 步长必须为正
    unit: str
    source_type: Literal["manual", "project_default"]
    confidence: float = Field(ge=0.0, le=1.0)
    source_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_supported_parameter(self) -> "EquipmentStep":
        """本阶段只支持电流、电压、速度（规范 §1）。"""
        if self.parameter_code not in SUPPORTED_PARAMETER_CODES:
            raise ValueError(
                f"不支持的参数 code：{self.parameter_code!r}，"
                f"仅支持 {SUPPORTED_PARAMETER_CODES}"
            )
        return self


class DeltaEstimate(BaseModel):
    """案例幅度估计结果（规范 §9）。

    raw_delta 为相似度加权中位数（带方向符号）；无有效差值时为 None，
    调用方按「正负一个设备步长」退化并标记 fallback_used。
    """

    parameter_code: str
    direction: str  # increase / decrease（来自图谱方向）
    raw_delta: float | None  # 加权中位数；None 表示退化为单步
    n_valid: int = 0  # 有效差值数（方向过滤后）
    n_filtered: int = 0  # 被方向过滤的差值数
    n_missing: int = 0  # 参数值缺失的支持案例数
    valid_deltas: list[float] = Field(default_factory=list)  # 有效差值（带符号，供大小标签用）
    dispersion: float | None = None  # MAD/|中位数|（0—1）；样本不足时为 None
    support_case_ids: list[str] = Field(default_factory=list)
    fallback_used: bool = False  # 无有效差值，退化为单设备步长


class ParameterAdjustment(BaseModel):
    """单参数修正量（规范 §5）。数值由程序计算，LLM 不得改动。"""

    parameter_code: str
    direction: Literal["increase", "decrease", "same"]
    raw_delta: float  # 原始修正量（案例加权中位数，或明确标记的单步退化值）
    quantized_delta: float  # 按设备步长量化后的修正量
    step: float = Field(gt=0)  # 设备步长（来自图谱）
    magnitude: Literal["small", "medium", "large"]
    support_case_ids: list[str] = Field(default_factory=list)  # 幅度支持案例
    path_ids: list[str] = Field(default_factory=list)  # 图谱关系 id（仅来自被选中的完整路径）
    source_refs: list[str] = Field(default_factory=list)  # 教材/说明书/数据表定位
    fallback: bool = False  # 是否使用单步退化值（规范 §9.3）
    support: float = Field(default=0.0, exclude=True, ge=0.0, le=1.0)  # 内部中间量：案例支持折算分（规范 §13）

    @model_validator(mode="after")
    def _check_consistency(self) -> "ParameterAdjustment":
        """模型层自检：受支持参数、方向与符号一致、步长整数倍。"""
        if self.parameter_code not in SUPPORTED_PARAMETER_CODES:
            raise ValueError(
                f"不支持的参数 code：{self.parameter_code!r}，"
                f"仅支持 {SUPPORTED_PARAMETER_CODES}"
            )
        if self.direction == "increase" and self.quantized_delta <= 0:
            raise ValueError("方向为 increase 时 quantized_delta 必须为正")
        if self.direction == "decrease" and self.quantized_delta >= 0:
            raise ValueError("方向为 decrease 时 quantized_delta 必须为负")
        if self.direction == "same" and self.quantized_delta != 0:
            raise ValueError("方向为 same 时 quantized_delta 必须为 0")
        ratio = abs(self.quantized_delta) / self.step
        if abs(ratio - round(ratio)) > 1e-6:
            raise ValueError(
                f"quantized_delta={self.quantized_delta} 不是步长 {self.step} 的整数倍"
            )
        return self


class ConfidenceBreakdown(BaseModel):
    """置信度分项（规范 §13），分项均为 0—1。"""

    similarity: float = Field(ge=0.0, le=1.0)  # 基准案例检索分数
    knowledge: float = Field(ge=0.0, le=1.0)  # 采用路径的关系置信度及来源完整度
    case_support: float = Field(ge=0.0, le=1.0)  # 有效支持案例数量和差值离散程度
    consensus: float = Field(ge=0.0, le=1.0)  # 与其他候选推荐值的接近程度
    equipment: float = Field(ge=0.0, le=1.0)  # 满足步长、范围和模式限制的程度


class AdjustmentKnowledgePath(BaseModel):
    """完整知识路径（验收规范 P0）：一条独立的
    Condition-SUGGESTS_ADJUSTMENT→Parameter→Mechanism→[Mechanism]→Quality 链。

    路径独立返回、不与其他路径展平合并；path_id 由节点/关系序列确定性
    生成；LLM 只能选择给定路径 ID，非法 ID 拒绝并告警。
    """

    path_id: str
    parameter_code: str
    node_codes: list[str]  # Condition → Parameter → Mechanism → ... → Quality
    relationship_ids: list[str]  # 路径上按顺序排列的关系 id
    confidence: float = Field(ge=0.0, le=1.0)  # 路径关系置信度均值
    source_refs: list[str] = Field(default_factory=list)  # 合并去重后的证据引用

    @model_validator(mode="after")
    def _check_structure(self) -> "AdjustmentKnowledgePath":
        if len(self.relationship_ids) != len(self.node_codes) - 1:
            raise ValueError("关系数必须等于节点数减 1（路径必须首尾相连）")
        if self.parameter_code not in SUPPORTED_PARAMETER_CODES:
            raise ValueError(
                f"不支持的参数 code：{self.parameter_code!r}，"
                f"仅支持 {SUPPORTED_PARAMETER_CODES}"
            )
        return self


class AdjustmentProposal(BaseModel):
    """单个基准案例生成的修正方案（规范 §5）。"""

    proposal_id: str
    base_case_id: str
    adjustments: list[ParameterAdjustment]
    # 基准案例参数值：供推荐值一致性校验与下游评审溯源
    base_current_a: float | None = None
    base_voltage_v: float | None = None
    base_speed_mm_s: float | None = None
    recommended_current_a: float | None = None
    recommended_voltage_v: float | None = None
    recommended_speed_mm_s: float | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_breakdown: ConfidenceBreakdown
    basis: str = ""  # 简短依据摘要，不含思维过程
    warnings: list[str] = Field(default_factory=list)
    valid: bool = True  # 越界方案判为无效（规范 §10）
    low_confidence: bool = False  # 该候选低于配置阈值（状态标记；全局提示只针对最高分方案）

    @model_validator(mode="after")
    def _check_recommended_consistency(self) -> "AdjustmentProposal":
        """推荐值必须等于基准值加量化修正量（由程序计算）。"""
        base_fields = {
            "welding_current": "base_current_a",
            "arc_voltage": "base_voltage_v",
            "welding_speed": "base_speed_mm_s",
        }
        recommend_fields = {
            "welding_current": "recommended_current_a",
            "arc_voltage": "recommended_voltage_v",
            "welding_speed": "recommended_speed_mm_s",
        }
        for adj in self.adjustments:
            base_value = getattr(self, base_fields[adj.parameter_code])
            recommended = getattr(self, recommend_fields[adj.parameter_code])
            if recommended is None:
                raise ValueError(
                    f"参数 {adj.parameter_code} 存在修正量但缺少推荐值"
                )
            if base_value is None:
                raise ValueError(
                    f"参数 {adj.parameter_code} 存在修正量但缺少基准案例值"
                )
            expected = base_value + adj.quantized_delta
            if abs(recommended - expected) > 1e-6:
                raise ValueError(
                    f"{adj.parameter_code} 推荐值 {recommended} 不等于 "
                    f"基准值 {base_value} + 量化修正量 {adj.quantized_delta}"
                )
        return self


class AdjustmentResult(BaseModel):
    """修正量推荐输出契约（规范 §15）。"""

    requirement_id: str
    equipment_id: str
    proposals: list[AdjustmentProposal]  # 全部有效候选，按置信度降序
    selected_proposal_id: str | None
    warnings: list[str] = Field(default_factory=list)
