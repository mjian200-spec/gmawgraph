# 焊接参数修正量生成：编程规范

## 1. 目标

在已完成的相似案例检索与图谱路径召回之后，实现最小闭环：

```text
需求 + 多个相似案例 + 图谱知识 + 设备步长
→ 多个独立修正方案 → 置信度排序 → 推荐参数
```

基本原则：

1. 图谱决定可调参数、调整方向和设备步长；
2. 案例数据估计修正量的相对大小；
3. Python 完成统计、量化和评分；
4. LLM 只选择路径和生成简短说明，不得凭空报数。

本阶段只处理焊接电流、电压和焊接速度。

## 2. 最小流程

1. 输入焊接需求和 `equipment_id`；
2. 检索相似度最高的 5 个案例；
3. 选择最多 3 个不同的基准案例；
4. 每个基准案例独立、并行生成一个方案；
5. 从图谱取得调整方向、知识路径和设备步长；
6. 从其他相似案例估计原始修正量；
7. 按设备步长量化并检查设备约束；
8. 计算置信度，保留多个方案并排序。

不得先平均多个案例再生成唯一方案。

## 3. 职责边界

| 模块 | 负责内容 |
|---|---|
| 图谱 | 参数方向、因果路径、设备步长、来源引用 |
| 案例库 | 相似工况、已观察参数差值 |
| Python | 加权中位数、量化、约束检查、置信度 |
| LLM | 路径选择、依据摘要 |

LLM 不得生成未由程序计算的参数值和置信度。

## 4. 设备步长

步长保存在 `Equipment-LIMITS→Parameter` 关系中。

取值优先级：

1. `adjustment_step`：说明书或设备配置给出的正式步长；
2. `default_step`：图谱显式保存的项目默认值；
3. 均不存在：停止生成该参数并返回告警。

当前原型：

| 参数 | 步长 | 属性 | 来源 |
|---|---:|---|---|
| 焊接电流 | 5 A | `adjustment_step` | CrobotpOS 第40页 |
| 焊接电压 | 0.2 V | `adjustment_step` | CrobotpOS 第40页 |
| 焊接速度 | 0.5 mm/s | `default_step` | 训练案例最小观测分辨率 |

速度默认值只是项目量化规则，不是制造商限值。
代码中禁止硬编码这三个数值，必须查询图谱。

## 5. 数据模型

```python
class EquipmentStep(BaseModel):
    parameter_code: str
    step: float
    unit: str
    source_type: Literal["manual", "project_default"]
    confidence: float
    source_refs: list[str]

class ParameterAdjustment(BaseModel):
    parameter_code: str
    direction: Literal["increase", "decrease", "same"]
    raw_delta: float
    quantized_delta: float
    step: float
    magnitude: Literal["small", "medium", "large"]
    support_case_ids: list[str]
    path_ids: list[str]
    source_refs: list[str]

class ConfidenceBreakdown(BaseModel):
    similarity: float
    knowledge: float
    case_support: float
    consensus: float
    equipment: float

class AdjustmentProposal(BaseModel):
    proposal_id: str
    base_case_id: str
    adjustments: list[ParameterAdjustment]
    recommended_current_a: float | None
    recommended_voltage_v: float | None
    recommended_speed_mm_s: float | None
    confidence: float
    confidence_breakdown: ConfidenceBreakdown
    basis: str
    warnings: list[str]
```

## 6. 必须实现的函数

```python
def get_adjustment_steps(
    equipment_id: str,
    store: Neo4jStore,
) -> dict[str, EquipmentStep]: ...

def select_diverse_base_cases(
    matches: list[CaseMatch], count: int = 3,
) -> list[CaseMatch]: ...

def estimate_case_delta(
    base_case: CaseRecord,
    support_cases: list[CaseMatch],
    parameter_code: str,
    direction: str,
) -> DeltaEstimate: ...

def quantize_delta(raw_delta: float, step: float) -> float: ...

async def generate_case_proposal(
    requirement: WeldingRequirement,
    base_case: CaseMatch,
    support_cases: list[CaseMatch],
    steps: dict[str, EquipmentStep],
    store: Neo4jStore,
) -> AdjustmentProposal: ...

async def generate_adjustment_recommendations(
    requirement: WeldingRequirement,
    equipment_id: str,
    top_k: int = 5,
    proposal_count: int = 3,
) -> list[AdjustmentProposal]: ...
```

同时提供同步包装函数，供 CLI 与测试调用。

## 7. 基准案例选择

案例按已有相似度排序后去重：

- 材料、厚度、接头、位置、气体和焊丝直径完全相同，只留最高分；
- 优先保留与需求差异类型不同的案例；
- 最多选择 3 个基准案例；
- 少于 3 个仍可输出，但降低案例支持和一致性得分。

## 8. 调整方向

需求与案例差异先转换为 `Condition`，例如：

```text
目标厚度更大 → condition:thickness_increase
目标改为立焊 → condition:position_to_vertical
目标焊丝更粗 → condition:wire_diameter_increase
```

查询链路：

```text
Condition-SUGGESTS_ADJUSTMENT→Parameter
Parameter-AFFECTS→Mechanism-AFFECTS→Quality
Equipment-LIMITS→Parameter
```

只修正有图谱方向支持的参数。
同一参数存在方向冲突时，停止该参数并写入 `warnings`。

## 9. 案例幅度估计

对每个待调参数计算：

```text
observed_delta = support_case.parameter - base_case.parameter
```

支持案例优先满足同一焊接方法、材料一致，且接头、位置、气体、焊丝直径尽量一致。
案例所体现的工况变化方向必须与当前需求差异可比较。

处理方法：

1. 删除符号与图谱方向冲突的差值；
2. 用相似度加权中位数聚合剩余差值；
3. 没有有效差值时，退化为正负一个设备步长；
4. 使用退化值时降低 `case_support`。

```text
raw_delta = weighted_median(observed_deltas, similarity_scores)
```

## 10. 步长量化

```python
step_count = max(1, round(abs(raw_delta) / step))
quantized_delta = direction_sign * step_count * step
recommended_value = base_value + quantized_delta
```

要求：

- 量化后仍保持图谱方向；
- 浮点数按步长小数位舍入；
- 量化后检查范围和控制模式；
- 越界方案判为无效，不做静默截断。

一元模式下，电压是随电流匹配的增益值。
无法可靠换算时，不得按独立伏特值自动修正。

## 11. 相对大小标签

数值修正量是主结果，大小标签仅用于说明：

- 至少 3 个有效案例差值：按绝对值的 33% 和 67% 分位点划分；
- 样本不足：1 个设备步为 `small`，2—3 步为 `medium`，4 步以上为 `large`；
- 使用步数规则时降低案例支持得分。

## 12. 并行生成

最多同时生成 3 个基准案例方案：

```python
results = await asyncio.gather(*proposal_tasks)
```

并发内容仅限图谱读取、案例读取和可选 LLM 路径选择，不并发写库。
汇总后恢复确定性顺序，再计算方案间一致性。

## 13. 置信度

置信度由程序计算，权重存入 `config/adjustment.yaml`：

```yaml
confidence_weights:
  similarity: 0.30
  knowledge: 0.25
  case_support: 0.15
  consensus: 0.15
  equipment: 0.15
```

分项均为 0—1：

- `similarity`：基准案例检索分数；
- `knowledge`：采用路径的关系置信度及来源完整度；
- `case_support`：有效支持案例数量和差值离散程度；
- `consensus`：与其他候选推荐值的接近程度；
- `equipment`：满足步长、范围和模式限制时为 1。

`confidence = Σ(weight × score)`。
返回最高分方案，同时保留其他有效方案。
最高分低于配置阈值时标记为低置信度建议。

## 14. 溯源

每个参数修正必须包含：

1. 基准案例 `base_case_id`；
2. 幅度支持案例 `support_case_ids`；
3. 图谱关系 `path_ids`；
4. 教材、说明书或数据表定位 `source_refs`；
5. 设备步长和 `source_type`。

Neo4j 不保存证据原文，只保存定位引用。
`basis` 输出简短摘要，不输出模型隐藏思维过程。

## 15. 查询与 CLI

新增查询函数：

```python
get_equipment_limits(equipment_id, parameter_codes)
get_adjustment_paths(condition_codes, parameter_codes)
get_path_sources(path_ids)
```

新增命令：

```bash
python scripts/recommend_adjustment.py \
  --requirement data/example_requirement.json \
  --equipment crobotpos_arc_module \
  --top-k 5 --proposal-count 3
```

输出至少包含：

```text
requirement_id, equipment_id, proposals,
selected_proposal_id, warnings
```

`proposals` 保留全部有效候选及置信度分解，供下一阶段强模型评审。

## 16. 最小测试

1. 电流从图谱读取 5 A 步长；
2. 电压从图谱读取 0.2 V 步长；
3. 速度读取图谱默认步长 0.5 mm/s；
4. 代码中不存在参数步长常量；
5. 与图谱方向冲突的案例差值被过滤；
6. 异常案例不显著影响加权中位数；
7. 修正量为设备步长的整数倍；
8. 3 个不同案例能并行产生候选；
9. 并行后结果顺序可重复；
10. 置信度总分等于配置加权和；
11. 每项修正均有案例、路径和来源引用；
12. 一元模式电压不会被误作独立伏特值；
13. 无步长、方向冲突或越界时返回告警。

## 17. 完成标准

```bash
python scripts/import_graph.py welding_kg_seed/graph_seed.json
python scripts/import_cases.py welding_kg_seed/cases.json
python scripts/recommend_adjustment.py \
  --requirement data/example_requirement.json \
  --equipment crobotpos_arc_module
pytest -q
```

至少输出 2 个有效候选。每个方案均满足：

- 修正方向来自图谱；
- 修正幅度来自案例或明确标记的单步退化；
- 步长来自图谱；
- 依据可追溯；
- 最终按程序置信度排序。
