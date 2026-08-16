# 焊接知识图谱最简原型编程规范

## 1. 目标与边界

本文档直接交给编程 Agent，实现焊接知识图谱第 1—5 项任务。
只验证以下最小闭环：

```text
焊接需求 → 最相似案例 → 需求差异 → 图谱多跳路径 → LLM 路径选择
```

抽取基线模型为 Qwen3-32B，但本阶段不做 PDF/OCR 自动抽取。
Neo4j 不保存证据原文，只保存外部引用 `source_refs`。
本阶段不实现参数修正量、最终推荐、强模型评审、模拟数据和正式测试。

## 2. 技术栈

- Python 3.11、Pydantic v2、PyYAML；
- Neo4j 5.x、官方 Python Driver；
- BGE-M3 用于案例文本相似度；
- Qwen3-32B 和 OpenAI 兼容接口用于路径选择；
- pytest 仅预留目录和配置。

不引入 APOC、消息队列、工作流平台、前端或微服务。

## 3. 本阶段交付

1. 项目骨架、配置和数据模型；
2. Neo4j 多图层模式、约束和索引；
3. 外部图规则 JSON 导入函数；
4. 外部案例 JSON 导入函数；
5. 最相似案例查询函数；
6. 需求与案例差异计算函数；
7. 固定模板的多跳路径查询函数；
8. Qwen3-32B 候选路径选择函数；
9. 可串联上述能力的命令行 Demo。

## 4. 多图层本体

| 图层 | 标签 | 唯一键 | 用途 |
|---|---|---|---|
| 案例层 | `Case` | `case_id` | 历史工艺案例 |
| 工况层 | `Condition` | `code` | 工况概念和差异条件 |
| 参数层 | `Parameter` | `code` | 可调焊接参数 |
| 机理层 | `Mechanism` | `code` | 电弧、熔滴和熔池机理 |
| 质量层 | `Quality` | `code` | 尺寸、性能和缺陷 |
| 设备层 | `Equipment` | `equipment_id` | 焊机能力和限制 |

`Mechanism.layer` 仅允许 `arc`、`transfer`、`pool`。
关系白名单：

```text
Case-HAS_CONDITION→Condition
Case-HAS_PARAMETER→Parameter
Case-HAS_RESULT→Quality
Condition-SUGGESTS_ADJUSTMENT→Parameter
Parameter-AFFECTS→Mechanism
Mechanism-AFFECTS/DETERMINES→Mechanism
Mechanism-AFFECTS→Quality
Equipment-LIMITS→Parameter
```

## 5. 属性约定

概念节点至少含 `code/name`、`definition`、`source_refs`。
设备节点使用 `equipment_id` 替代 `code`。
知识关系可含 `source_change`、`target_change`、`condition_text`、`confidence`、`source_refs`。
证据引用格式示例：`["GMAW:p142:c001", "CrobotpOS:p38:t002"]`。
`Case` 节点直接保存以下检索字段：

```text
case_id, process, material, thickness_mm, joint_type, position,
wire_diameter_mm, shielding_gas, welding_current_a,
welding_voltage_v, welding_speed_mm_s, retrieval_text, embedding
```

案例仍通过 `HAS_*` 关系连接概念节点，用于路径展示和扩展。

## 6. 外部 JSON 契约

`graph_seed.json` 顶层包含 `nodes` 和 `relationships`：

| 对象 | 必填字段 |
|---|---|
| node | `id`、`label`、`properties` |
| relationship | `id`、`type`、`from`、`to`、`properties` |

节点示例：`{"id":"condition:thickness_increase","label":"Condition","properties":{"code":"thickness_increase","name":"板厚增加","source_refs":[]}}`。
关系示例：`{"id":"rule:001","type":"SUGGESTS_ADJUSTMENT","from":"condition:thickness_increase","to":"parameter:welding_current","properties":{"target_change":"increase","source_refs":[]}}`。
`cases.json` 顶层包含 `cases`，每项包含第 5 节的案例检索字段，以及
`conditions`、`parameters`、`results` 三个节点引用数组。
示例引用为 `"condition:flat_position"`、`"parameter:welding_current"`。
`id` 是文件内稳定引用；数据库唯一键按第 4 节定义。
两个文件必须先通过 Pydantic 和白名单校验，再开启写事务。

## 7. 项目结构

```text
welding-kg/
├── pyproject.toml, README.md, .env.example
├── config/schema.yaml, config/retrieval.yaml
├── data/seed/
├── src/welding_kg/
│   ├── models.py, settings.py, neo4j_store.py
│   ├── graph_importer.py, case_retriever.py, case_comparator.py
│   ├── path_retriever.py, path_planner.py, service.py
├── scripts/init_graph.py, import_graph.py, import_cases.py, run_demo.py
└── tests/
```

## 8. 数据模型

`models.py` 至少定义 `WeldingRequirement`、`CaseRecord`、`DifferenceItem`、
`CaseMatch`、`ReasoningPath`、`PathSelection`、`ImportResult`、`DemoResult`。
未通过模型校验的数据不得传入数据库层或 LLM 层。

## 9. 必须实现的函数

数据库层：

```python
class Neo4jStore:
    def verify_connectivity(self) -> None: ...
    def initialize_schema(self) -> None: ...
    def execute_read(self, query: str, params: dict) -> list[dict]: ...
    def execute_write(self, query: str, params: dict) -> list[dict]: ...
    def close(self) -> None: ...
```

通用 Cypher 函数只供内部模块使用，不暴露给 LLM。

导入层：

```python
def import_graph_json(path: str, store: Neo4jStore) -> ImportResult: ...
def import_case_json(path: str, store: Neo4jStore) -> ImportResult: ...
```

导入顺序固定为读取、模型校验、白名单校验、标准化、事务写入、报告。
节点和关系使用 `MERGE`；重复导入不得产生重复数据。
`ImportResult` 返回新增数、更新数、跳过数和错误列表。

案例查询与差异层：

```python
def get_case(case_id: str, store: Neo4jStore) -> CaseRecord | None: ...
def find_similar_cases(
    requirement: WeldingRequirement, store: Neo4jStore, top_k: int = 5
) -> list[CaseMatch]: ...
def compare_case(
    requirement: WeldingRequirement, case: CaseRecord
) -> list[DifferenceItem]: ...
```

先按 `process` 过滤，再进行混合相似度排序。
差异由程序计算，不使用 LLM。
数值变化为 `increase/decrease/same`，类别变化为 `changed/same`。
差异代码格式为 `{field}_{change}`，如 `thickness_increase`。

路径查询层：

```python
def query_reasoning_paths(
    differences: list[DifferenceItem],
    store: Neo4jStore,
    target_quality: str | None = None,
    limit_per_difference: int = 5,
) -> list[ReasoningPath]: ...
```

固定查询链为 `Condition → Parameter → Mechanism → [Mechanism] → Quality`。
首段关系为 `SUGGESTS_ADJUSTMENT`，其余为 `AFFECTS/DETERMINES`。
机理内部允许 0—2 跳，总路径不得超过 6 条关系。
按差异代码、终点质量和节点序列去重，并合并关系的 `source_refs`。
另行查询 `Equipment-LIMITS→Parameter`，将限制附加到路径结果。

LLM 路径选择层：

```python
def select_reasoning_paths(
    requirement: WeldingRequirement,
    base_case: CaseRecord,
    differences: list[DifferenceItem],
    candidates: list[ReasoningPath],
) -> PathSelection: ...
```

Qwen3-32B 只返回候选中的 `path_id` 和简短理由。
结果必须通过 Pydantic 校验；不存在的 `path_id` 直接拒绝。
不得输出思维链，也不得生成图中不存在的路径。

## 10. 相似案例算法

结构化字段：材料、板厚、接头、位置、焊丝直径、保护气体。
类别字段相同得 1，不同得 0；数值字段使用：

```text
score = max(0, 1 - abs(a-b) / max(abs(a), abs(b), 1e-6))
final_score = 0.8 * structured_score + 0.2 * semantic_score
```

字段权重和混合权重写入 `retrieval.yaml`。
缺失字段不计分，其余权重重新归一化。
BGE-M3 不可用时退化为纯结构化检索，并返回 warning。
`CaseMatch` 返回总分、结构化分、语义分和字段分项；LLM 不参与排序。

## 11. Neo4j 实现规则

- 创建六类唯一约束和必要索引，初始化可重复运行；
- 动态标签和关系必须先映射到白名单；
- 所有属性值通过 Cypher 参数传入；
- 每个输入文件使用一个事务，非法引用导致整体回滚；
- 路径查询使用固定 Cypher，不执行 LLM 生成的 Cypher；
- 数据库连接从环境变量读取，不提交真实密码。

## 12. 配置和服务入口

`.env.example` 至少包含：

```text
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=change_me
LLM_BASE_URL=http://localhost:8000/v1
LLM_MODEL=qwen3-32b
EMBEDDING_MODEL=BAAI/bge-m3
```

`schema.yaml` 保存白名单和唯一键；`retrieval.yaml` 保存过滤、权重和 Top-K。
服务入口：

```python
def run_reasoning_demo(
    requirement: WeldingRequirement, top_k: int = 5
) -> DemoResult: ...
```

执行顺序：检索案例、选最高分案例、计算差异、查询路径、选择路径、返回结果。
没有案例或路径时返回空列表和 warning，不抛出不可读错误。

## 13. 命令行与输出

```bash
python scripts/init_graph.py
python scripts/import_graph.py data/seed/graph_seed.json
python scripts/import_cases.py data/seed/cases.json
python scripts/run_demo.py examples/requirement.json
```

成功退出码为 0；输入、校验或连接错误退出码非 0。
Demo 以 UTF-8 JSON 输出 `case_matches`、`base_case`、`differences`、
`candidate_paths`、`selected_path_ids`、`selection_reason`、`warnings`。

## 14. 构建顺序

1. 项目骨架：依赖、配置、模型和 Neo4j 连接；
2. 图谱模式：白名单、约束、索引和存储类；
3. JSON 导入：先图规则，后案例，保证幂等和回滚；
4. 案例检索：结构化评分、BGE-M3、混合排序和差异；
5. 路径召回：固定 Cypher、设备限制、去重和 LLM 选择；
6. Demo 串联：README 写明安装、配置、导入和运行命令。

## 15. 最低验收标准

- 项目可安装并通过静态导入检查；
- Neo4j 初始化可重复运行；
- 两类 JSON 导入严格校验且幂等；
- 能检索 Top-K 案例并返回分项得分；
- 能读取案例并计算结构化差异；
- 能按差异查询一条或多条有限多跳路径；
- LLM 只能选择数据库已返回的路径；
- 证据原文不进入 Neo4j；
- 无数据场景返回清晰 warning；
- README 包含完整执行命令。

## 16. 下一任务边界

框架完成后另开任务：从两本 PDF 摘取最小模拟数据，生成 `graph_seed.json` 和
`cases.json`，再设计案例检索、差异、多路径的单元测试和端到端测试。
当前编程 Agent 不得自行虚构书中知识或案例，也不得提前实现参数推荐。
