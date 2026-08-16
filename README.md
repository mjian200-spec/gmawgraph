# GMAWGraph — 焊接知识图谱最简原型（第一阶段）

验证最小闭环：

```text
焊接需求 → 最相似案例 → 需求差异 → 图谱多跳路径 → LLM 路径选择
```

本阶段不做 PDF/OCR 自动抽取、参数修正量、最终推荐与正式测试；
Neo4j 不保存证据原文，只保存外部引用 `source_refs`。

## 环境与依赖

- conda 环境：`/ENV/Anaconda/envs/jm/GMAWGraph`（Python 3.11），`vllm==0.19.1` 部署大模型
- Neo4j 5.17 Community：`/DATA/jm/neo4j/GMAWGraph`（Bolt 7200 / HTTP 7201）
- 大模型：Qwen3-32B（vLLM OpenAI 兼容接口，端口 8000）
- 嵌入模型：BGE-M3（vLLM 嵌入接口，端口 8001）

启动服务（若未运行）：

```bash
# 一键管理 Neo4j + 两个 vLLM 服务
bash scripts/manage_services.sh start    # start / stop / status / restart
```

## 安装与配置

```bash
conda activate /ENV/Anaconda/envs/jm/GMAWGraph
cd /CODE/jm/0Project_crp/GMAWGraph
pip install -e .
cp .env.example .env   # 按实际环境修改（默认值与本机一致）
```

## 执行命令（规范 §13）

```bash
# 1. 初始化图谱模式（唯一约束 + 索引，可重复执行）
python scripts/init_graph.py

# 2. 导入图谱规则（节点 + 关系，先通过 Pydantic 与白名单校验）
python scripts/import_graph.py data/seed/graph_seed.json

# 3. 导入历史案例（引用节点必须已存在；自动计算 BGE-M3 嵌入）
python scripts/import_cases.py data/seed/cases.json

# 4. 端到端推理演示（默认输出易读中文报告，--json 输出规范 §13 原始契约）
python scripts/run_demo.py examples/requirement.json
python scripts/run_demo.py examples/requirement.json --json
```

`examples/requirement.json` 的值域与 mini_test 真实数据契约一致
（规范化映射见 `mini_test/source_validation.md` §2.2：工艺 GMAW、
材料 carbon_steel 等英文 code；自匹配词表与案例库不一致时检索会返回空）。

成功退出码为 0；输入、校验或连接错误退出码非 0。

## 多图层本体（规范 §4）

| 图层 | 标签 | 唯一键 | 用途 |
|---|---|---|---|
| 案例层 | `Case` | `case_id` | 历史工艺案例 |
| 工况层 | `Condition` | `code` | 工况概念和差异条件 |
| 参数层 | `Parameter` | `code` | 可调焊接参数 |
| 机理层 | `Mechanism` | `code` | 电弧、熔滴和熔池机理（layer ∈ arc/transfer/pool） |
| 质量层 | `Quality` | `code` | 尺寸、性能和缺陷 |
| 设备层 | `Equipment` | `equipment_id` | 焊机能力和限制 |

关系白名单：`HAS_CONDITION`、`HAS_PARAMETER`、`HAS_RESULT`、
`SUGGESTS_ADJUSTMENT`、`AFFECTS`、`DETERMINES`、`LIMITS`
（完整端点约束见 `config/schema.yaml`）。

## 外部 JSON 契约（规范 §6）

- `graph_seed.json`：`{nodes: [{id, label, properties}], relationships: [{id, type, from, to, properties}]}`
- `cases.json`：`{cases: [{case_id, process, ...检索字段, retrieval_text, conditions, parameters, results}]}`
- 证据引用格式：`["GMAW:p142:c001", ...]`；`id` 是文件内稳定引用

## 相似案例算法（规范 §10）

先按 `process` 过滤，再混合打分：

```text
score = max(0, 1 - abs(a-b) / max(abs(a), abs(b), 1e-6))   # 数值字段
final = 0.8 * structured + 0.2 * semantic                   # BGE-M3 余弦
```

字段权重与 Top-K 见 `config/retrieval.yaml`；BGE-M3 不可用时退化为
纯结构化检索并返回 warning。

## 差异代码约定（制作种子数据的硬性契约）

差异代码由 `compare_case` 机械生成：`{字段别名}_{变化}`。图谱中
`Condition` 节点的 `code` 必须与该集合逐字一致，否则路径查询返回空。
别名映射见 `case_comparator.py` 的 `_FIELD_CODE_ALIAS`，有效集合：

```text
数值字段（increase / decrease / same）：
  thickness_increase | thickness_decrease | thickness_same
  wire_diameter_increase | wire_diameter_decrease | wire_diameter_same

类别字段（changed / same）：
  material_changed | material_same
  joint_type_changed | joint_type_same
  position_changed | position_same
  shielding_gas_changed | shielding_gas_same
```

## 冒烟实验（本机验证）

合成冒烟数据位于 `data/smoke/`（**全部为合成测试数据，非书中知识**）：
所有唯一键带 `smoke_` 前缀，与真实数据（mini_test）零冲突，
且运行结束自动清理、不在库中留测试数据。覆盖导入幂等、检索排序、
差异计算、路径查询与 LLM 选择：

```bash
python scripts/run_smoke.py        # 合成数据冒烟（12 项）
python scripts/test_mini_data.py   # 真实模拟数据测试（导入核验/检索/路径/LLM）
```

报告输出到 `data/smoke/report/`。

## 最低验收标准（规范 §15）

- [x] 项目可安装并通过静态导入检查
- [x] Neo4j 初始化可重复运行
- [x] 两类 JSON 导入严格校验且幂等
- [x] 能检索 Top-K 案例并返回分项得分
- [x] 能读取案例并计算结构化差异
- [x] 能按差异查询一条或多条有限多跳路径
- [x] LLM 只能选择数据库已返回的路径
- [x] 证据原文不进入 Neo4j
- [x] 无数据场景返回清晰 warning
- [x] README 包含完整执行命令

## 文档结构化预处理（welding_kg.docprep，原 DocProduce 已融合）

OCR 解析结果 → 按标题组织、保持阅读顺序的结构化文档（`data/docprep/`
生成物，.gitignore 已忽略），供文本抽取与后续 VLM 处理。本模块不理解
图片/表格工艺内容；文档处理的规范与验收见
`src/welding_kg/docprep/`（任务书 v2、README、CLAUDE.md）。

```bash
# 只读审计 / 完整预处理（含 33 张表格图片的 PDF 补裁）/ 当前输出校验
python scripts/preprocess_document.py audit
python scripts/preprocess_document.py run
python scripts/preprocess_document.py validate

# 回归测试（17 条验收标准）与资源完整性检查
python -m pytest -q tests/test_docprep.py
python tests/check_docprep_assets.py
```

- 主输出 `data/docprep/document_structure.json`：sections（content 有序流：
  text_segment / figure / table / subsection），递归遍历恢复完整阅读顺序；
  figure/table 项含 `asset_path`（parsed_asset 或 pdf_crop）、`caption`、
  `bbox` 与 `source_ref`。
- 图谱衔接：Neo4j 只保存外部引用 `source_refs`；`source_registry.json`
  提供 source_ref → PDF 页码/bbox 回溯，与图谱证据引用约定一致。
- 统计：章节 427、文本段 788、图片 741、表格 193（含 33 张 pdf_crop）。
