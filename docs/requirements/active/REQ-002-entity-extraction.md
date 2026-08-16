---
requirement_id: REQ-002
title: GMAW关键名词与实体规范化
status: approved
version: 1.0.0
created_at: 2026-08-16
updated_at: 2026-08-16
depends_on:
  - REQ-001
supersedes: null
repository: mjian200-spec/gmawgraph
baseline_branch: main
implementation_status: not_started
---

# GMAW关键名词发现、语境解释与图谱实体规范化任务书

## 1. 任务目标

在现有`GMAWGraph`仓库内，基于`welding_kg.docprep`生成的结构化GMAW文档，实现“全模态关键名词发现—语境召回—大模型解释与消歧—图谱实体规范化”流水线：

1. 使用Qwen3.5-9B（VLM）完整浏览正文、图片和表格，并行发现与焊接知识图谱相关的关键名词；
2. 汇总名词在全书中的全部提及，并为每个名词召回跨章节、跨内容类型的证据文本段；
3. 使用Qwen3-32B基于证据包解释名词在不同情景下的具体含义，识别一词多义和同义表达；
4. 区分名称相近但含义不同、上下位或仅相关的名词；
5. 将消歧后的标准实体映射到工况层、参数层、机理层、质量层和设备层；
6. 输出可由后续关系抽取、参数窗口构建和现有`graph_importer`适配器直接复用的标准实体ID、定义、别名、层级及证据。

本任务不是通用NER，也不是关系抽取。最终链路为：

```text
结构化文档
→ Qwen3.5-9B全模态关键名词发现
→ 名词提及汇总与语境召回
→ Qwen3-32B多情景含义解释
→ 同义词归并与近义词区分
→ 多层图谱实体映射
→ 后续条件化关系抽取
```

## 2. 工作模式与授权边界

```text
模式：在现有仓库中实现与验证

允许：
- 读取`data/docprep/`下的结构化输出、文本、图片、表格和溯源注册表；
- 在`src/welding_kg/term_extraction/`内新建独立的关键名词抽取与术语规范化程序；
- 调用已部署的Qwen3.5-9B（VLM）和Qwen3-32B；
- 复用现有BGE-M3服务构建术语语境索引、证据包、人工核验样本和评估报告；
- 输出标准实体和图谱层级映射。

禁止：
- 修改或覆盖`welding_kg.docprep`的任何输入、输出和资源；
- 让Qwen3-32B脱离召回证据、仅凭模型常识定义术语；
- 将字符串相似直接视为同义词；
- 抽取实体关系、变化方向、条件规则或工艺参数窗口；
- 直接写入Neo4j；
- 将未消歧的名词直接当作唯一图谱节点；
- 改变现有案例检索`config/retrieval.yaml`的语义或权重；
- 改变现有推理、修正量生成或`graph_importer`行为。
```

## 3. 仓库基线与权威输入

### 3.1 仓库事实

执行智能体必须先以仓库`main`分支的实际代码为准复核以下事实，若有变化则在实现报告中说明，不得静默套用本任务书：

| 现有位置 | 已有职责 | 本任务的处理方式 |
|---|---|---|
| `src/welding_kg/docprep/` | OCR/PDF到章节化文档 | 只读消费，不在其中加入知识抽取逻辑 |
| `src/welding_kg/models.py` | 案例、路径、导入等核心Pydantic模型 | 不堆入术语阶段中间模型；新模型放到`term_extraction/models.py` |
| `src/welding_kg/settings.py` | Neo4j、Qwen3-32B、BGE-M3配置 | 仅兼容性扩展VLM配置和新配置加载器 |
| `config/schema.yaml` | 图谱节点/关系白名单与唯一键 | 作为最终节点映射的权威约束，不在本阶段改关系白名单 |
| `config/retrieval.yaml` | 现有案例检索配置 | 保持不变；术语召回使用独立配置文件 |
| `src/welding_kg/case_retriever.py` | 案例混合检索及BGE-M3调用 | 可抽取通用嵌入客户端，但不得改变案例检索结果 |
| `src/welding_kg/graph_importer.py` | `graph_seed.json`校验与Neo4j写入 | 仅做输出兼容性验证，本任务不调用写库入口 |
| `scripts/manage_services.sh` | Neo4j、Qwen3-32B、BGE-M3服务 | 先审计GPU和端口再决定是否扩展Qwen3.5服务管理 |
| `tests/` | 离线单测与真实服务集成测试 | 沿用`integration`标记和“不可跳过即通过”的规则 |

当前图谱节点白名单为`Case`、`Condition`、`Parameter`、`Mechanism`、`Quality`、`Equipment`。本阶段只产生后五类概念实体；`Case`仍由案例数据构建。

### 3.2 上游输入

| 输入 | 路径 | 用途 | 异常处理 |
|---|---|---|---|
| 主结构文件 | `data/docprep/document_structure.json` | 按`section_order`和`content[].order`读取文本、图片和表格 | 停止，不从辅助文件重建主结构 |
| 溯源注册表 | `data/docprep/source_registry.json` | 将`source_ref`解析为页码、`bbox`和资源路径 | 停止涉及图谱就绪输出的处理 |
| 物理内容块 | `data/docprep/normalized_blocks.jsonl` | 必要时回查融合前`raw_text`/`normalized_text` | 记录限制，不替代主结构 |
| 预处理报告 | `data/docprep/preprocessing_report.json` | 获取质量统计、残留问题和预处理版本 | 报告缺失，不得假定无异常 |
| 字段说明 | `src/welding_kg/docprep/README.md` | 确认下游遍历、视觉资源和溯源接口 | 停止输入适配器开发 |
| Qwen3.5-9B（VLM） | 启动时确认实际模型ID和端点 | 全模态关键名词发现 | 不得猜测接口或退化为纯文本模式 |
| Qwen3-32B | `.env`中的`LLM_*`，当前默认`:8000` | 语境解释、词义消歧和图谱分类 | 不得在无证据包时调用定案 |
| BGE-M3 | `.env`中的`EMBEDDING_*`，当前默认`:8001` | 术语语境的稠密召回 | 不可用时不得把降级结果冒充完整证据召回 |
| 图谱本体 | `config/schema.yaml` | 约束节点标签、唯一键和机理子层 | 发现契约冲突时停止图谱就绪输出 |

上游已经完成章节结构、阅读顺序、题注关联、缺失表格补裁及溯源处理。本任务不得重新实现文档解析。

`asset_path`按仓库根目录解析；表格的`ocr_html`只作辅助文本，VLM仍须读取表格原图。不得向`data/docprep/`写入抽取结果。

### 3.3 服务配置隔离

保留现有环境变量含义：`LLM_*`继续指向Qwen3-32B，`EMBEDDING_*`继续指向BGE-M3。新增：

```text
VLM_BASE_URL
VLM_MODEL
VLM_API_KEY
```

Qwen3.5-9B不得占用已有`:8000`或`:8001`。若后续智能体修改`scripts/manage_services.sh`，必须先确认可用GPU、模型实际路径、端口和显存配置；未确认时只提供配置接入与服务健康检查，不擅自硬编码启动命令。

## 4. 多层图谱实体契约

### 4.1 图谱层级

| 图谱层 | 关键名词范围 | 后续用途 |
|---|---|---|
| 工况层 | 焊接方法、材料、板厚、接头、位置、保护气体、焊丝等文档概念 | 规则适用条件、案例和参数窗口条件 |
| 参数层 | 电流、电压、焊接速度、送丝速度、干伸长、气体流量等 | 参数推荐输入、输出和调整目标 |
| 机理层 | 电弧、熔滴过渡、熔池、热输入、受热状态等 | 参数到质量的解释路径 |
| 质量层 | 熔深、熔宽、余高、成形、飞溅和焊接缺陷等 | 推荐目标、质量结果和风险 |
| 设备层 | 焊机、控制模式、功率、参数范围和调节能力等 | 限制推荐结果的可执行性 |

`Case`由现有案例导入流程构建，`ProcessWindow`尚未进入当前`schema.yaml`。二者都不是本阶段的输出节点；后续若新增`ProcessWindow`，必须通过单独的本体变更任务，不得由实体抽取脚本私自增加标签。

`Condition`同时承载“文档工况概念”和系统计算出的“需求差异条件”。本任务只抽取前者，并写入`condition_kind=document_concept`；`DifferenceItem.code`生成的`thickness_increase`等差异代码属于`condition_kind=difference`，不得因名称相似被覆盖、合并或复用。

### 4.2 图谱实体契约

在关键名词抽取前冻结`graph_entity_contract.json`。每个实体类型至少定义：

```json
{
  "type_id": "Parameter",
  "graph_layer": "parameter",
  "node_label": "Parameter",
  "definition": "可设置、测量或记录的焊接过程量",
  "include_examples": ["焊接电流", "电弧电压", "焊接速度"],
  "exclude_examples": ["熔深", "咬边"],
  "downstream_roles": [
    "rule_subject",
    "rule_object",
    "process_window_field",
    "case_field"
  ],
  "version": "v1"
}
```

Qwen3.5-9B使用该契约判断名词是否与图谱相关，但只给出候选类型或候选层。Qwen3-32B根据跨情景证据完成最终词义、类型和图谱层判定。节点映射必须遵循现有唯一键：`Condition`/`Parameter`/`Mechanism`/`Quality`使用`code`，`Equipment`使用`equipment_id`；`Mechanism.layer`必须为`arc`、`transfer`或`pool`。

### 4.3 分层优先级

```text
P0：工况层、参数层
    直接决定推荐条件、参数窗口和规则适用性，优先保证召回率和标准化精度。

P1：机理层、质量层、设备层
    支撑可解释路径、质量目标和设备限制，优先保证词义区分准确性。

P2：外围操作、标准名称和低频术语
    保留候选，但不阻塞P0/P1核心实体库冻结。
```

## 5. 总体技术架构

```text
document_structure.json
        │
        ▼
全模态抽取单元构建
        │
        ▼
Qwen3.5-9B并行浏览文本/图片/表格
        │
        ▼
关键名词提及＋视觉文字转录＋初步相关性
        │
        ▼
名词表面规范化＋提及倒排索引
        │
        ▼
精确匹配/BM25/语义检索/章节多样化召回
        │
        ▼
每个名词或候选词组的跨情景证据包
        │
        ▼
Qwen3-32B词义归纳、提及消歧、同义/近义判定、图谱分层
        │
        ▼
标准实体＋提及映射＋图谱就绪实体＋人工审核队列
```

Qwen3.5-9B负责“看全、找全”；检索算法负责“为每个名词找齐语境”；Qwen3-32B负责“看懂、分清、归层”。

## 6. 阶段A：启动审计与接口冻结

执行：

1. 读取README并验证`document_structure.json`的实际Schema；
2. 抽样检查`text_segment`、`figure`、`table`和`subsection`；
3. 确认`subsection`引用不会造成重复遍历；
4. 验证`source_ref`、`asset_path`、页码和`bbox`；
5. 确认两种模型的端点、上下文长度、图像输入、结构化输出和并发限制；
6. 确认图谱实体契约和类型集合；
7. 冻结输入、模型输出、证据包和标准实体Schema；
8. 确认模型响应缓存和断点续跑策略。

输出到`data/term_extraction/runs/<run_id>/audit/`：

```text
input_audit.json
model_capabilities.json
effective_config.json
schema_snapshot.json
```

任一核心接口未确认时，不得进入全量模型调用。

实现时固定读取`document_structure.meta.document_version`和`preprocess_version`，并把它们写入每次运行清单。输入审计还必须验证当前仓库基线统计：427个章节、788个文本段、741个图片项、193个表格项；统计变化不必自动判错，但必须阻止复用旧缓存，要求生成新`run_id`。

## 7. 阶段B：构建全模态抽取单元

通过`src/welding_kg/docprep/model.py`定义的字段，将章节级`content[]`转换成稳定、不重复的`extraction_units.jsonl`。

```json
{
  "unit_id": "unit:...",
  "section_id": "...",
  "heading_path": ["..."],
  "content_order": 12,
  "content_type": "text_segment|figure|table",
  "text": "...",
  "caption": "...",
  "ocr_html": "...",
  "asset_path": "...",
  "raw_block_refs": ["..."],
  "source_refs": ["..."],
  "context_before": "...",
  "context_after": "..."
}
```

约束：

- 遵循原文顺序，但模型调用可按单元并行；
- `subsection`只用于导航，不重复生成子章节内容；
- 按`document_structure.section_order`建立章节索引，递归时必须维护已访问集合；
- 图片输入包含图题、标题路径和必要相邻正文；
- 表格输入包含原图、表题、OCR HTML和必要表注；
- 文本上下文不得跨越不相关章节；
- 相同输入和配置下`unit_id`稳定；
- 不覆盖上游`raw_text`和资源文件。

输出：`data/term_extraction/runs/<run_id>/units/extraction_units.jsonl`。生成后必须断言有效内容项总数与输入一致；当前基线应为1722个可抽取单元（788文本、741图片、193表格），`subsection`不计入模型单元。

## 8. 阶段C：Qwen3.5-9B全模态关键名词发现

### 8.1 抽取目的

Qwen3.5-9B扫描全部有效文本、图片和表格，追求关键名词的高召回，同时通过图谱实体契约过滤普通名词和无图谱价值内容。

### 8.2 关键名词判定

抽取以下候选：

- 可成为五层图谱节点的术语；
- 能限定案例、参数窗口或规则适用范围的名词；
- 可作为后续影响关系主体或对象的名词；
- 文中具有定义、别名、缩写或专门区分说明的名词；
- 图、表中对理解焊接机理、参数、质量或设备有作用的标签。

排除：

- 与焊接知识无关的普通名词；
- 单独数值、页码、序号和单位；
- 只表示篇章结构的词；
- 没有独立概念意义的残缺OCR字符串；
- 装饰性图片中的文字。

### 8.3 输出Schema

```json
{
  "mention_id": "mention:...",
  "unit_id": "unit:...",
  "surface_term": "焊接速度",
  "normalized_surface": "焊接速度",
  "content_type": "text_segment|figure|table",
  "char_span": {"start": 0, "end": 4},
  "visual_locator": null,
  "local_context": "...",
  "provisional_type": "Parameter",
  "provisional_layers": ["parameter"],
  "graph_relevance": "high|medium|low",
  "relevance_reason": "...",
  "visual_transcription": null,
  "source_refs": ["..."],
  "asset_path": null,
  "model_id": "...",
  "prompt_version": "...",
  "confidence": 0.0,
  "status": "candidate"
}
```

要求：

- 文本提及必须能在输入文本中精确定位；
- 图片和表格提及必须绑定实际资源和`source_ref`；
- 图内文字转录单独保存，并标记为VLM派生文本；
- 候选类型和层级只是检索与优先级提示，不是最终结论；
- 不在此阶段合并同义词或解释最终含义；
- 不抽取实体关系。

### 8.4 并行运行

- 按`unit_id`建立任务队列；
- 在模型服务允许范围内并行调用；
- 各工作进程写独立分片，完成后按`unit_id`确定性合并；
- 按`content_hash＋model_id＋prompt_version＋schema_version`缓存响应；
- 支持失败单元重试和断点续跑；
- 不因并发改变输出ID或阅读顺序。

输出到`data/term_extraction/runs/<run_id>/discovery/`：

```text
qwen35_term_mentions.part-*.jsonl
qwen35_term_mentions.jsonl
qwen35_errors.jsonl
qwen35_runtime_report.json
```

## 9. 阶段D：名词候选汇总与索引

### 9.1 表面规范化

允许自动处理：

- 全角/半角；
- 英文大小写；
- 明确的空格、连字符和编号格式差异；
- 已确认的繁简变体；
- 单位字符格式。

表面规范化只能形成检索键，不能证明概念相同。对有上下文证据的OCR错误，记录`observed_surface`、`corrected_surface`、`correction_reason`、`source_refs`和置信度；修正只影响抽取产物，不回写`data/docprep`。OCR错误写入`ocr_corrections.jsonl`并作为提及变体，不得登记为正式同义词。

### 9.2 名词候选

为每个不同表面名词生成稳定`term_candidate_id`，关联全部提及：

```json
{
  "term_candidate_id": "term:welding_speed",
  "surface_forms": ["焊接速度"],
  "mention_ids": ["..."],
  "provisional_types": ["Parameter"],
  "provisional_layers": ["parameter"],
  "section_ids": ["..."],
  "source_refs": ["..."],
  "frequency": 0,
  "modalities": ["text", "figure", "table"],
  "priority": "P0|P1|P2"
}
```

不得在该阶段把名称不同的候选自动归为同一实体。

### 9.3 检索语料索引

建立统一检索语料，包含：

- `text_segment`正文；
- 章节标题和`heading_path`；
- 图题、表题和表注；
- 表格OCR HTML的可读文本；
- Qwen3.5-9B从图片中读取的文字和局部描述。

VLM派生文字必须带`derived_from_vlm=true`，不能与原始OCR或正文混淆。

输出到`data/term_extraction/runs/<run_id>/retrieval/`：

```text
term_candidates.jsonl
term_mentions_index.jsonl
retrieval_corpus.jsonl
ocr_corrections.jsonl
```

持久索引写入`data/term_extraction/indexes/<corpus_version>/`，其中`corpus_version`必须包含文档版本、预处理版本、语料构建版本和嵌入模型ID。

## 10. 阶段E：为名词召回跨情景证据

### 10.1 召回目标

不是简单找出包含该字符串的段落，而是为Qwen3-32B组织能够回答以下问题的证据：

- 该名词在不同章节是否表示同一含义；
- 它属于工况、参数、机理、质量还是设备；
- 文中是否存在明确或隐含定义；
- 是否存在全称、简称、英文缩写或替代表达；
- 哪些名称相近但测量对象、作用范围或物理含义不同；
- 哪些条件会改变该名词的具体指代。

### 10.2 混合召回

每个名词至少使用：

1. **精确召回**：原始表面词、规范化写法和所有已知提及；
2. **BM25召回**：查找包含相同词根、缩写或领域搭配的段落；
3. **语义召回**：使用项目可用的嵌入模型检索定义性和相似语境；
4. **结构增强**：提高标题、图题、表题、表头和定义句权重；
5. **跨章节多样化**：避免Top-K全部来自同一段或重复表格；
6. **跨模态补充**：加入图片/表格提及及其相邻正文；
7. **候选词组召回**：为可能同义或近义的多个名词共同召回对比证据。

中文BM25分词必须可复现：保留拉丁字母、数字、单位和连字符Token，对连续中文使用领域词典最长匹配并补充2-gram，不依赖在线分词服务。精确召回先查`surface_forms`的原文和规范化形式；稠密召回输入应拼接名词、候选层、标题路径和局部语境，不把整章无差别嵌入为一个向量。

稠密召回默认复用现有`.env`中的BGE-M3 OpenAI兼容接口；客户端能力可以从`case_retriever.py`安全抽取为共享模块，但必须用回归测试证明现有案例检索得分、排序和降级行为不变。术语召回参数写入`config/term_retrieval.yaml`，不得复用或修改`config/retrieval.yaml`。

可采用RRF融合不同检索器的排名，避免在没有试点数据时主观指定不可解释的线性权重。

同义/近义候选阻塞至少使用“规范化字符串、缩写/全称模式、共享提及语境、BGE相似度、候选层”五类信号的并集；跨层候选不能被过滤掉，因为它们是发现一词多义的重要来源。阻塞阶段只生成待比较组，不输出语义结论。

### 10.3 证据包

```json
{
  "bundle_id": "bundle:term:welding_speed",
  "term_candidate_ids": ["term:welding_speed"],
  "surface_forms": ["焊接速度"],
  "query_variants": ["焊接速度"],
  "contexts": [
    {
      "context_id": "context:...",
      "section_id": "...",
      "heading_path": ["..."],
      "content_type": "text_segment|figure|table",
      "text": "...",
      "source_refs": ["..."],
      "retrieval_channels": ["exact", "bm25", "dense"],
      "retrieval_rank": 1,
      "derived_from_vlm": false
    }
  ],
  "coverage": {
    "sections": 0,
    "modalities": [],
    "direct_mentions": 0,
    "definition_candidates": 0
  }
}
```

证据包需要在Qwen3-32B当前32768上下文上限内预留提示词和结构化输出空间，并兼顾直接提及、定义性语境、不同章节、不同模态和对比名词。配置使用Token预算，不以字符数替代；重复或近似重复段落应去重。

### 10.4 召回评估

在人工试点集上评估：

- 直接提及覆盖率；
- 定义证据Recall@K；
- 不同词义证据Recall@K；
- 同义/近义对比证据Recall@K；
- 章节和模态多样性；
- 重复证据比例；
- 单个证据包的平均Token数。

召回不足时先修复检索，不得把证据缺失交给Qwen3-32B用常识补齐。

输出：`data/term_extraction/runs/<run_id>/retrieval/term_context_bundles.jsonl`、`retrieval_metrics.json`和无法满足最低证据覆盖的`retrieval_review_queue.jsonl`。

## 11. 阶段F：Qwen3-32B多情景含义解释

### 11.1 第一轮：词义归纳

对每个名词证据包执行：

- 归纳它在文档中出现的不同含义；
- 为每个含义生成独立`sense_id`；
- 将每个`mention_id`分配到具体`sense_id`；
- 为每个词义提取或生成有证据支持的定义；
- 分析词义成立的章节、工艺和语境范围；
- 给出候选实体类型和图谱层；
- 证据不足时输出`UNRESOLVED`。

```json
{
  "sense_id": "sense:voltage:arc",
  "term_candidate_id": "term:voltage",
  "sense_name": "电弧电压",
  "definition": "...",
  "definition_status": "explicit|synthesized|provisional|missing",
  "definition_source_refs": ["..."],
  "scope_description": "...",
  "mention_ids": ["..."],
  "entity_type": "Parameter",
  "graph_layer": "parameter",
  "node_label": "Parameter",
  "downstream_roles": ["rule_subject", "rule_object"],
  "confidence": 0.0,
  "status": "candidate|unresolved"
}
```

同一个表面名词可以产生多个词义实体。例如“电压”若在证据中分别指电弧电压、电源电压或控制系统电压，必须拆分，不能共享一个标准实体ID。

### 11.2 第二轮：候选词组对比

Qwen3-32B不得只逐词独立解释。对检索算法生成的候选词组或实体对，联合输入各自证据包，判断：

```text
EXACT_ALIAS
CONTEXTUAL_ALIAS
BROADER_THAN
NARROWER_THAN
RELATED_NOT_ALIAS
DISTINCT
UNRESOLVED
```

输出必须说明：

- 共同含义；
- 区别维度；
- 是否能在所有语境互换；
- 层级是否一致；
- 适用范围；
- 支持判断的`source_refs`。

区别维度至少考虑：

```text
measurement_object
physical_quantity
process_scope
control_context
value_kind（设定值/实际值/范围）
granularity
graph_layer
```

### 11.3 第三轮：图谱分层审核

根据解释后的具体词义，而不是表面名词，将每个`sense_id`映射到：

```text
condition
parameter
mechanism
quality
equipment
```

规则：

- 每个词义设置一个`primary_graph_layer`；
- 允许记录`secondary_roles`，但不能用多层标签掩盖未消歧问题；
- 同一表面词跨层使用时优先拆分词义；
- 无法确定层级时进入人工审核，不强制归类；
- 图谱层和`node_label`必须来自`graph_entity_contract.json`。

输出到`data/term_extraction/runs/<run_id>/interpretation/`：`term_senses.jsonl`、`mention_sense_links.jsonl`、`term_semantic_decisions.jsonl`、`qwen32_errors.jsonl`和运行统计。模型输出先通过Pydantic校验，再进行引用闭包校验；模型给出的未知`mention_id`、`sense_id`或`source_ref`一律拒绝，不得自动补造。

## 12. 阶段G：标准实体与同义表达

### 12.1 标准实体

标准实体以`sense_id`为基础构建，而不是直接以字符串为基础：

```json
{
  "entity_id": "entity:parameter:arc_voltage",
  "canonical_name": "电弧电压",
  "entity_type": "Parameter",
  "primary_graph_layer": "parameter",
  "node_label": "Parameter",
  "graph_unique_key": {"name": "code", "value": "arc_voltage"},
  "properties": {
    "code": "arc_voltage",
    "name": "电弧电压",
    "definition": "...",
    "source_refs": ["..."]
  },
  "downstream_roles": [
    "rule_subject",
    "rule_object",
    "process_window_field",
    "case_field"
  ],
  "definition": "...",
  "definition_status": "explicit|synthesized|provisional|missing",
  "definition_source_refs": ["..."],
  "aliases": [
    {
      "name": "...",
      "alias_type": "EXACT_ALIAS|CONTEXTUAL_ALIAS",
      "scope": "...",
      "source_refs": ["..."]
    }
  ],
  "near_entities": [
    {
      "entity_id": "...",
      "relation": "BROADER_THAN|NARROWER_THAN|RELATED_NOT_ALIAS|DISTINCT",
      "distinction": "...",
      "source_refs": ["..."]
    }
  ],
  "sense_ids": ["..."],
  "mention_ids": ["..."],
  "source_refs": ["..."],
  "status": "candidate|reviewed|approved"
}
```

`Equipment`的`graph_unique_key.name`改为`equipment_id`。`Mechanism.properties`必须额外带合法`layer`。`Condition.properties`必须带`condition_kind=document_concept`。`entity_id`是抽取流水线的稳定主键，图谱唯一键是对现有Neo4j契约的适配键，两者不得混为同一字段。

### 12.2 自动与人工判定边界

允许自动归并：

- 全角/半角、大小写、空格和连接符等机械变体；
- 文档明确陈述的全称—简称；
- 有多个一致证据支持且无语境冲突的固定缩写。

必须人工复核：

- 跨章节才出现的同义候选；
- 只由模型推断、原文未明确支持的同义表达；
- P0/P1实体的语义合并；
- 同一表面词拆出的多个词义；
- 名称相近但可能属于不同层的实体；
- Qwen3-32B输出`UNRESOLVED`或证据矛盾的候选。

OCR错误和乱码只作为提及变体记录，不进入正式别名列表。

### 12.3 图谱就绪门槛

只有同时满足以下要求的实体才进入`graph_ready_entities.jsonl`：

- 已分配稳定`entity_id`；
- 已确认具体词义；
- 已确认实体类型、主图谱层和节点标签；
- 至少绑定一个可解析的文档提及；
- 同义词和近义词冲突已经处理；
- 具有非空定义；定义可以是有证据的综合表述，但必须标明`synthesized`并绑定来源；
- 状态达到`reviewed`或`approved`。

后续关系抽取必须通过`mention_id → sense_id → entity_id`复用本任务输出，禁止重新创建自然语言节点。

另生成只读预览`graph_seed_nodes_preview.json`，顶层严格满足现有`GraphSeedFile`的`{nodes, relationships}`结构，`relationships`固定为空数组。概念节点必须包含现有导入器要求的唯一键、`name`、`definition`和`source_refs`；该文件只用于调用`graph_importer._validate_seed`做兼容性测试，本阶段CLI不得连接Neo4j或调用`import_graph_json`。

标准实体、审核队列和图谱预览写入`data/term_extraction/runs/<run_id>/output/`与`review/`，不得写入`data/seed/`，以避免被现有导入脚本误用。

## 13. 速度与质量平衡

### 13.1 核心分工

```text
Qwen3.5-9B：全书、全模态、一次主扫描，追求覆盖率
检索算法：聚合重复提及并为每个不同名词构建证据包
Qwen3-32B：按不同名词/候选词组调用，追求语义判断质量
人工审核：只处理高影响合并、跨层冲突和未决项
```

Qwen3-32B不得按每个原始内容块重新抽取，也不得按每个重复提及分别解释。其调用粒度是“唯一名词证据包”或“候选词组证据包”。

### 13.2 性能优化

- Qwen3.5-9B按抽取单元并行，结果分片后确定性合并；
- Qwen3-32B按名词证据包并行，但同一候选词组只提交一次；
- 高频重复名词先聚合，再解释；
- P0/P1名词优先进入32B，P2长尾可后续增量处理；
- 使用内容哈希、检索版本、模型版本和提示词版本缓存；
- 召回语境先去重和压缩，避免把重复段落传给32B；
- 通过候选阻塞生成同义/近义词组，禁止全词表笛卡尔积比较；
- 错误单元、错误证据包和未决实体支持独立重跑；
- 若只有某一图谱层质量不足，只重跑该层相关名词和证据包。

### 13.3 共同指标

```text
Qwen3.5阶段：units/min、VLM tokens/unit、关键名词Recall、相关性Precision、失败率
召回阶段：Recall@K、平均证据包Token、重复率、章节/模态覆盖
Qwen3-32B阶段：terms/min、tokens/term、词义准确率、同义词精度、近义词区分准确率
图谱阶段：分层准确率、提及绑定率、图谱就绪率、P0/P1未决比例
工程阶段：缓存命中率、重跑比例、人工审核率、总模型调用量
```

不能只报告总体F1或总耗时。质量必须按图谱层、名词频率和内容模态拆分。

### 13.4 自适应升级策略

| 候选状态 | 默认处理 | 质量保护 |
|---|---|---|
| P0/P1、证据充分、单一词义 | 一个名词证据包调用一次32B | 抽样人工复核，不按每个提及重复调用 |
| P0/P1、多词义或跨层冲突 | 扩大跨章节/跨模态召回后再次调用32B | 强制进入人工审核，禁止自动合并 |
| P2、低频、证据充分 | 批量或延迟解释 | 不阻塞核心实体库冻结 |
| 任意层、证据不足 | 停在`UNRESOLVED` | 不允许模型用常识补定义 |
| 同义候选 | 先阻塞分组，再做组内比较 | 禁止全量两两比较和字符串自动归并 |

运行报告必须同时给出以下规模关系，用来判断成本是否失控：

```text
Qwen3.5调用规模 ≈ 有效抽取单元数（当前基线1722，可在不丢内容的前提下批处理）
Qwen3-32B词义调用规模 ≈ 唯一名词候选数，而不是提及数
Qwen3-32B比较调用规模 ≈ 候选阻塞组数，而不是候选数平方
人工审核规模 ≈ 高影响冲突数＋未决数，而不是全部实体数
```

提示词批处理、Top-K、RRF窗口、证据Token预算、并发数和重试次数均放在配置中。先用试点曲线比较“质量增益/额外Token/额外耗时”，选择拐点后再冻结全量配置，不在任务书中凭经验硬编码阈值。

## 14. 试点与阶段门

### 14.1 试点集

至少构建：

```text
120个全模态抽取单元：60文本、30图片、30表格
50个名词证据包：覆盖高频、低频、一词多义和跨模态名词
30组同义/近义候选：覆盖同义、上下位、相关、不同和未决类别
```

### 14.2 阶段门

```text
Gate A：Qwen3.5关键名词召回与相关性通过人工审查
Gate B：检索证据的Recall@K和多样性通过审查
Gate C：Qwen3-32B词义、同义/近义和图谱分层结果通过审查
Gate D：Schema、溯源、缓存和确定性测试通过
Gate E：人工确认后才运行全量并冻结核心实体库
```

项目负责人尚未确定硬阈值时，先报告试点基线、错误样例和速度数据，不得自行宣称达到全量运行标准。

## 15. 并行实施关系

接口冻结后，可并行：

```text
工作流A：构建全模态抽取单元、Qwen3.5调用器和并行执行框架
工作流B：构建检索语料、混合召回算法和证据包Schema

共享接口：extraction_units.jsonl、graph_entity_contract.json、source_ref规则
汇合点：Qwen3.5试点提及进入检索索引后，验证完整证据包
```

试点提及稳定后，可并行：

```text
工作流C：按不同名词构建证据包
工作流D：构建Qwen3-32B结构化解释与候选词组对比调用器

共享接口：term_candidates.jsonl、term_context_bundle.schema.json
汇合点：首批50个证据包通过Schema和人工抽查后，运行32B试点
```

不能提前并行：

- 未形成Qwen3.5名词候选前，不能冻结检索查询词；
- 未验证召回质量前，不能让32B大规模解释；
- 未完成词义拆分前，不能形成最终同义词簇；
- 未确认图谱分层前，不能输出图谱就绪实体。

## 16. 仓库内实施落点

### 16.1 新增文件

```text
config/
  term_extraction.yaml
  term_retrieval.yaml
  graph_entity_contract.json

src/welding_kg/term_extraction/
  __init__.py
  models.py                 # 全部阶段的Pydantic v2契约
  document_adapter.py       # 只读消费docprep输出
  unit_builder.py           # 稳定抽取单元
  clients.py                # VLM/LLM调用、超时、重试、结构化响应
  discovery.py              # Qwen3.5全模态名词发现
  consolidation.py          # 表面规范化、提及聚合
  corpus.py                 # 检索语料构建
  retrieval.py              # exact/BM25/BGE-M3/RRF/多样化
  comparison.py             # 候选阻塞与词组生成
  interpretation.py         # Qwen3-32B词义归纳和术语对比
  entity_builder.py         # sense到标准实体
  graph_adapter.py          # schema.yaml映射及graph_seed节点预览
  cache.py                  # 内容寻址缓存和断点续跑
  validation.py             # Schema、溯源、确定性和覆盖检查
  pipeline.py               # 阶段编排，不含Neo4j写入
  prompts/
    qwen35_key_term_discovery.md
    qwen32_sense_induction.md
    qwen32_term_comparison.md
    qwen32_layer_classification.md

scripts/
  extract_terms.py          # audit/discover/retrieve/interpret/validate/run

tests/
  test_term_extraction_unit.py
  test_term_extraction_integration.py
  check_term_extraction_outputs.py
```

### 16.2 修改文件

| 文件 | 允许的最小改动 |
|---|---|
| `.env.example` | 增加`VLM_BASE_URL`、`VLM_MODEL`、`VLM_API_KEY`，不改现有变量 |
| `.gitignore` | 增加`data/term_extraction/`，避免模型产物和缓存入库 |
| `src/welding_kg/settings.py` | 增加VLM字段及两个术语配置加载器；保持现有默认值和函数行为 |
| `pyproject.toml` | 只在确有必要时加入检索/Token计数依赖，并锁定兼容版本 |
| `scripts/manage_services.sh` | 可选；仅在GPU、模型路径、端口经人工确认后扩展Qwen3.5管理 |
| 根`README.md`、`tests/README.md` | 增加运行命令、输出说明和测试分组 |

如需抽取通用BGE客户端，应新增共享模块并让`case_retriever.py`委托调用；不得复制一份行为逐渐分叉，也不得改变现有案例检索输出。除表中兼容性改动外，不应修改`docprep`、案例比较、路径检索、修正量生成和Neo4j存储模块。

### 16.3 运行产物

```text
data/term_extraction/
  runs/<run_id>/
    manifest.json
    audit/
    units/extraction_units.jsonl
    discovery/
    retrieval/
    interpretation/
    review/entity_review_queue.jsonl
    output/
      canonical_entities.json
      graph_ready_entities.jsonl
      graph_seed_nodes_preview.json
      mention_sense_links.jsonl
      term_semantic_decisions.jsonl
    reports/
      term_extraction_report.json
      term_extraction_report.md
  cache/
  indexes/<corpus_version>/
  current.json
```

`current.json`只能在整次运行通过验证后原子更新，指向已完成`run_id`；失败运行保留在自身目录，不污染当前有效输出。

### 16.4 推荐实施顺序

1. 先提交仓库审计、Pydantic模型、配置加载和`document_adapter`，只运行离线测试；
2. 实现稳定单元、缓存、Qwen3.5调用器和小规模`discover`试点；
3. 实现语料、exact/BM25/BGE-M3、RRF和证据包，先通过人工召回评估；
4. 实现Qwen3-32B词义归纳、候选词组比较和审核队列；
5. 实现实体构建、`schema.yaml`映射和只读`graph_seed`预览校验；
6. 通过三个质量Gate后再运行全量；最后补README、测试说明和完成报告。

每一步应形成独立、可回滚的提交。禁止把模型调用、语义合并、图谱适配和服务脚本改动堆在一个不可审查的提交中。

## 17. 最终交付物

| 交付物 | 内容 |
|---|---|
| 图谱实体契约 | `graph_entity_contract.json` |
| 全模态抽取单元 | `extraction_units.jsonl` |
| Qwen3.5关键名词提及 | `qwen35_term_mentions.jsonl` |
| 名词候选表 | `term_candidates.jsonl` |
| 提及索引 | `term_mentions_index.jsonl` |
| 检索语料与索引 | `retrieval_corpus.jsonl`、`retrieval_index/` |
| 名词证据包 | `term_context_bundles.jsonl` |
| 词义结果 | `term_senses.jsonl` |
| 提及—词义映射 | `mention_sense_links.jsonl` |
| 同义/近义判定 | `term_semantic_decisions.jsonl` |
| 标准实体库 | `canonical_entities.json` |
| 图谱就绪实体 | `graph_ready_entities.jsonl` |
| 图谱节点兼容预览 | `graph_seed_nodes_preview.json`，关系数组为空 |
| OCR提及修正台账 | `ocr_corrections.jsonl`，不回写预处理结果 |
| 人工审核队列 | `entity_review_queue.jsonl` |
| 运行和评估报告 | `term_extraction_report.json`及可读摘要 |
| 代码、配置和测试 | 调用器、检索器、Schema、缓存及端到端测试 |

## 18. 验收标准

1. 不修改`src/welding_kg/docprep/`及`data/docprep/`输出；
2. Qwen3.5-9B完整处理全部有效文本、图片和表格单元；
3. 并行运行不改变`unit_id`、`mention_id`或最终顺序；
4. 文本提及可精确定位，视觉提及关联真实`asset_path`和`source_ref`；
5. Qwen3.5候选不被直接当作最终标准实体；
6. 每个进入Qwen3-32B的名词都具有可审计的语境证据包；
7. 检索覆盖直接提及、定义语境、跨章节语境及候选词对比证据；
8. Qwen3-32B输出能够区分同一表面词的不同`sense_id`；
9. 每个词义和定义均关联证据`source_refs`；
10. 同义词判断区分完全同义、局部同义、上下位、相关、不同和未决；
11. 相似字符串不会在没有语义证据时自动合并；
12. 每个标准实体具有确定的类型、主图谱层、节点标签和下游角色；
13. 后续关系抽取可通过`mention_id → sense_id → entity_id`复用标准实体；
14. 未决、多义、跨层冲突和P0/P1语义合并进入人工审核队列；
15. Qwen3-32B调用数按不同名词或候选词组统计，不按原始提及数线性增长；
16. 所有输出通过JSON Schema，所有来源均可解析；
17. 缓存命中、错误重试和断点续跑均有测试；
18. 质量和速度按模型阶段、图谱层和模态分别报告；
19. 全量处理只在三个核心试点Gate通过后启动；
20. 输出不包含实体关系、条件规则、参数窗口或Neo4j写入逻辑。
21. `config/retrieval.yaml`保持字节级不变，现有案例检索回归测试全部通过；
22. 图谱就绪节点严格匹配`config/schema.yaml`的标签、唯一键及`Mechanism.layer`约束；
23. 文档工况实体不会覆盖或复用`DifferenceItem`生成的差异代码；
24. `graph_seed_nodes_preview.json`通过现有导入校验逻辑，且`relationships=[]`；
25. 运行产物、索引和缓存只写入`data/term_extraction/`，并被`.gitignore`排除；
26. Qwen3.5、Qwen3-32B和BGE-M3的端点、模型ID、提示词版本及调用统计均进入`manifest.json`；
27. 离线测试不连接任何模型、BGE或Neo4j，使用固定桩覆盖异常输出、重试、缓存和确定性；
28. 标记为`integration`的测试实际调用Qwen3.5、Qwen3-32B和BGE-M3，服务不可达时失败而不是跳过；Neo4j不属于本阶段集成测试依赖；
29. 以下命令均返回0：

```bash
/ENV/Anaconda/envs/jm/GMAWGraph/bin/python -m pytest -q -m "not integration"
/ENV/Anaconda/envs/jm/GMAWGraph/bin/python -m pytest -q tests/test_term_extraction_integration.py -m integration
/ENV/Anaconda/envs/jm/GMAWGraph/bin/python scripts/extract_terms.py validate --run-id <run_id>
```

## 19. 停止与升级条件

遇到以下情况停止相应阶段并报告：

- 主结构、注册表、资源路径或字段说明不一致；
- Qwen3.5-9B无法实际接收图片或表格图像；
- 全文并行扫描出现大量漏单元、重复单元或不可恢复错误；
- 图谱实体契约未确认，无法判断“关键名词”的相关性；
- 检索证据包无法覆盖已知不同词义；
- 证据包主要由重复段落组成或超出32B上下文预算；
- Qwen3-32B只能借助常识而不能基于证据作出解释；
- 同义词、近义词或图谱层存在无法消除的领域歧义；
- 为提升速度需要跳过P0/P1证据、溯源或人工审核；
- 需要修改`docprep`输入/输出、开始关系抽取或写入Neo4j；
- 需要改变`config/schema.yaml`节点/关系白名单才能容纳某个候选概念；
- 需要占用现有`:8000`、`:8001`或未经确认的GPU资源；
- 新增依赖会破坏Python 3.11、OpenAI 3.x或`httpx2`兼容约束。

## 20. 完成汇报格式

```markdown
## 完成情况
- 已完成阶段：
- 未完成阶段：
- 阻塞项：

## 输入与配置
- 文档结构版本：
- 图谱实体契约版本：
- Qwen3.5-9B模型ID与提示词版本：
- Qwen3-32B模型ID与提示词版本：
- 检索配置与索引版本：
- 运行ID：

## 数据统计
- 抽取单元总数：
- 文本/图片/表格单元数：
- 关键名词提及数：
- 不同名词候选数：
- 名词证据包数：
- 拆分词义数：
- 标准实体数：
- 同义词组数：
- 近义/不同实体对数：
- 各图谱层实体数：
- 图谱就绪实体数：
- 待人工审核数：

## 速度与资源
- Qwen3.5处理速度和调用量：
- 检索平均耗时和证据包Token数：
- Qwen3-32B调用量与处理速度：
- 缓存命中率和失败重跑率：
- 人工审核比例：

## 质量评估
- 关键名词召回与相关性：
- 检索Recall@K与证据多样性：
- 词义消歧准确率：
- 同义词和近义词判断：
- 图谱分层准确率：
- 定义证据覆盖率：
- Schema与溯源检查：

## 交付物
- 代码与配置：
- 数据输出：
- 测试与报告：

## 风险与后续
- 残留风险：
- 待人工决定：
- 是否可以进入条件化关系抽取阶段：是 / 否
```
