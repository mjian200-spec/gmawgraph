# GMAW 文档结构化预处理任务书（v2）

## 技术标准：供后续智能体执行时参考

### 1. 任务定位

#### 1.1 任务名称

GMAW OCR 解析结果结构化预处理：标题层级恢复 + 有序内容组织 + 视觉资源保留。

#### 1.2 核心目标

基于原始 PDF 及现有解析 JSON，开发一套可重复运行、可审计、可追溯的程序，把
OCR 解析结果转换为**按标题组织、保持原始阅读顺序、可供后续文本抽取和 VLM
处理的结构化文档**。

处理链路：

```text
OCR JSON
→ 内容块审计
→ 阅读顺序恢复
→ 标题识别和层级恢复
→ 每个内容块绑定 section_id
→ 连续文本块融合为 text_segment
→ 图片、表格和题注关联
→ 保留原始视觉资源
→ 生成"标题—有序内容"结构
→ 自动验证溯源、顺序和资源完整性
```

#### 1.3 下游接口

本任务的主输出为 `data/docprep/document_structure.json`（见第 4 节）。后续文本抽取
与 VLM 处理直接消费该文件：

- 文本抽取：遍历 `sections[].content` 中的 `text_segment` 项；
- VLM：读取 `figure` / `table` 项的 `asset_path` 加载图片，`caption` 为
  图题/表题，`bbox` 与 `source_ref` 用于溯源回原 PDF。

本阶段**不理解**图片和表格中的工艺知识。图片与表格的语义理解由后续 VLM 完成。

#### 1.4 与旧版任务书的关系

本任务书（v2）取代 v1 版任务书。v1 中的 OCR 数值人工修正工作流、表格
rowspan/colspan 展开、多级表头恢复、数值单位拆分、跨页表格语义确认与
视觉抽样验收门禁等，均**不属于本阶段功能**（见第 6 节删除清单）。

### 2. 范围边界

#### 2.1 本任务包含

- 章、节、小节及更小标题的层级和父子关系恢复；
- 每个正文、公式、列表、图片、表格块的 section_id / heading_path / 页码 /
  顺序 / bbox / source_ref / original_index 标注；
- 同一最深层 section_id 下、阅读顺序连续的文本块融合为 `text_segment`；
- 图片、表格与题注、脚注的版面关联（题注作为视觉项属性）；
- 视觉资源保留：有效图片路径校验；无效表格图片按原 PDF 的 page+bbox 补裁；
- 生成 `document_structure.json`（主输出）、`source_registry.json`、
  `preprocessing_report.json`；
- 自动验证：溯源、顺序、资源完整性、确定性。

#### 2.2 本任务明确不包含

- 理解图片或表格的工艺内容（由后续 VLM 完成）；
- OCR 数值人工修正工作流及修正应用；
- 表格行级结构规范化（rowspan/colspan 展开、多级表头、数值单位拆分、
  异常值推测）；
- 基于表头/单元格语义的跨页表格确认；
- 实体识别、关系抽取、工艺规则生成、参数推荐；
- 视觉抽样目检作为验收门禁。

### 3. 输入与启动检查

预期输入（执行前必须确认实际文件，不得猜测路径）：

- 原始 PDF：`GMAW/hybrid_ocr/GMAW(OCR)_origin.pdf`；
- 解析 JSON：`GMAW/hybrid_ocr/GMAW(OCR)_content_list_v2.json`；
- 解析器产生的图片资源：`GMAW/hybrid_ocr/images/`。

启动时记录：

```yaml
document_id: GMAW
document_version: v2
source_json: <实际路径>
source_pdf: <实际路径>
parser_name: hybrid_ocr
preprocess_version: <程序版本>
```

**解析 JSON 或原始 PDF 缺失时，必须停止并报告阻塞**（任务涉及 PDF 补裁与
溯源），不允许降级后继续产出看似完整的结果。

已知事实（供程序自检）：

- 741 个 image 块均有有效图片文件；
- 193 个 table 块中有 33 个 `image_source.path` 为 `"images/"`（目录，非有效
  文件），必须按原 PDF page+bbox 补裁表格区域图片。

### 4. 主输出结构

#### 4.1 `data/docprep/document_structure.json`

```json
{
  "meta": {
    "document_id": "GMAW",
    "document_version": "v2",
    "source_json": "...",
    "source_pdf": "...",
    "parser_name": "hybrid_ocr",
    "preprocess_version": "2.1.0"
  },
  "sections": [
    {
      "section_id": "gmaw:v2:sec:2.1.1",
      "title": "2.1.1 气体放电的基本概念",
      "level": 3,
      "parent_section_id": "gmaw:v2:sec:2.1",
      "heading_path": ["第2章 焊接电弧", "2.1 电弧的物理基础", "2.1.1 气体放电的基本概念"],
      "start_source_ref": "...",
      "end_source_ref": "...",
      "start_page": 17,
      "end_page": 18,
      "heading_block_id": "...",
      "content": [
        {
          "item_type": "text_segment",
          "item_id": "gmaw:v2:item:seg0017",
          "order": 1,
          "text": "...",
          "member_block_ids": ["..."],
          "source_refs": ["..."],
          "sources": [{"pdf_page": 17, "bbox": [1, 2, 3, 4], "source_ref": "..."}]
        },
        {
          "item_type": "figure",
          "item_id": "gmaw:v2:item:fig0009",
          "order": 2,
          "asset_path": "...",
          "original_asset_path": "...",
          "asset_origin": "parsed_asset",
          "asset_exists": true,
          "sha256": "...",
          "mime_type": "image/jpeg",
          "caption": "图2-1 直流放电电路",
          "footnotes": [],
          "pdf_page": 17,
          "bbox": [644, 495, 870, 612],
          "source_ref": "...",
          "section_id": "gmaw:v2:sec:2.1.1",
          "heading_path": ["..."]
        },
        {
          "item_type": "table",
          "item_id": "gmaw:v2:item:tab0003",
          "order": 3,
          "asset_path": "...",
          "original_asset_path": "...",
          "asset_origin": "pdf_crop",
          "asset_exists": true,
          "sha256": "...",
          "mime_type": "image/png",
          "caption": "表 2-2 ...",
          "footnotes": [],
          "ocr_html": "<table>...</table>",
          "pdf_page": 19,
          "bbox": [117, 379, 887, 701],
          "source_ref": "...",
          "section_id": "gmaw:v2:sec:2.1.1",
          "heading_path": ["..."]
        },
        {
          "item_type": "subsection",
          "item_id": "gmaw:v2:sec:2.1.1.1",
          "order": 4,
          "section_id": "gmaw:v2:sec:2.1.1.1"
        }
      ],
      "children": ["gmaw:v2:sec:2.1.1.1"]
    }
  ],
  "section_order": ["gmaw:v2:sec:root", "gmaw:v2:sec:1", "..."]
}
```

约定：

- `content` 为该标题范围内的**有序内容流**：直接属于该标题的文本、图片和
  表格放入 `content`；遇到子标题时插入 `subsection` 引用项，子标题自己的
  内容只保存在子 section 中，不在父 section 重复；
- **递归遍历 `section.content` 必须能恢复完整文档阅读顺序**（`section_order`
  为文档顺序的章节平铺列表，便于校验）；
- `title` 只保存**拆分后的标题文本**；若标题与正文粘在同一 OCR 块，
  标题字段不得包含正文，剩余正文必须作为文本块进入该标题的 content；
- 每个视觉项必须携带第 4.3 节的资产记录字段；
- `ocr_html` 仅为辅助字段（输入中存在时原样保留），**不是原图的替代品**，
  本阶段不判断其内容是否正确。

#### 4.2 其它输出

| 文件 | 用途 |
|---|---|
| `data/docprep/source_registry.json` | 所有 source_ref → pdf_page/bbox；figure/table 项还必须包含可用 asset_path |
| `data/docprep/preprocessing_report.json` | 输入统计、标题/章节统计、文本段统计、图片表格资源统计、缺失资源与 PDF 补裁统计、无法确定的标题边界与阅读顺序问题、自动验收结果 |
| `data/docprep/normalized_blocks.jsonl` | 内部中间结果（审计与溯源用）：每个物理块的原始与规范化文本、section_id 等。**不是主接口**，下游不依赖其稳定格式 |

#### 4.3 视觉资源记录

每个视觉资源至少记录：

```json
{
  "asset_path": "<可用路径>",
  "original_asset_path": "<解析 JSON 中记录的原始路径>",
  "asset_origin": "parsed_asset | pdf_crop",
  "asset_exists": true,
  "sha256": "...",
  "mime_type": "image/jpeg | image/png"
}
```

- `parsed_asset`：来自 OCR JSON 的 `image_source.path`，解析为实际存在的
  文件路径（不复制、不修改原图）；
- `pdf_crop`：输入图片缺失时，按原 PDF 的 page+bbox（加少量边距）补裁，
  输出到 `data/docprep/assets/pdf_crop/`；
- `asset_path` 禁止是目录；程序必须校验文件存在、可读、可计算哈希。

### 5. 各阶段要求

#### 5.1 内容块审计

统计：解析页数与 PDF 页数、各类型块数量、空文本块/重复块/异常短块、
字段完整性（bbox/type）、标题层级字段分布、无题注图/表数量、印刷页码映射、
表格 HTML 存在性、图片资源有效性分类（有效文件 / 目录 / 缺失）。

#### 5.2 内容块标准化

标准块类型：

```text
heading / paragraph / list_item / formula / table / table_title / table_note /
figure / figure_title / page_header / page_footer / page_number /
page_footnote / page_aside_text / unknown
```

- `raw_text` 必须原样保留，不得覆盖；清洗只写入 `normalized_text`；
- 页眉/页脚/页码从下游内容中排除，但不得从原始解析中删除；
- 标题与正文粘在同一 OCR 块时：标题字段只保存拆分后的标题，剩余正文生成
  文本块并进入该标题的 content，`raw_text` 原样保留；无法可靠拆分时标记
  `needs_review`，不得把整段正文写入 section.title。

#### 5.3 阅读顺序与页码

- `pdf_page` 为主页码，`printed_page` 为辅助页码；
- 页面内顺序综合原解析顺序与 bbox（多栏版式按栏聚类），置信度不足时标记
  `layout_review_required`；
- `block_order` 为页内序，`global_order` 为全文档序（结构输出中体现）。

#### 5.4 标题层级与章节树

- 标题层级按编号启发式（`第X章=1 / X.Y=2 / X.Y.Z=3 / N.=4 / （N）=5 /
  N）=6`）+ 上下文归父（最近低层级）重新判定；
- 章节 `end_source_ref / end_page` 必须结束于**下一个同级或更高级标题之前**
  （结束于其范围内最后一个内容块）；不得出现所有章节都结束于文档最后一页
  的错误；
- 每个内容块直接保存 `section_id`（最深层归属），不依赖 heading_path 字符串
  反向匹配；
- 标题跨行合并、无编号标题、前辅文与附录均需可解释处理。

#### 5.5 文本融合

- 将同一最深层 section_id 下、阅读顺序连续的文本块（paragraph / list_item /
  formula / page_footnote 等）融合为 `text_segment`；
- 融合边界：图片、表格、子标题；**不得跨过视觉内容拼接文本**；
- 跨页可以融合，但必须确实连续且中间没有标题、图片或表格；
- 保留 `member_block_ids`、`source_refs` 与逐块 `sources`
  （pdf_page/bbox/source_ref），保证可完整还原到原始块；
- **不允许为了"通顺"而生成、改写或补充内容**。

#### 5.6 题注与脚注关联

- 图题/表题作为对应视觉项的 `caption` 属性；表脚注/图脚注作为 `footnotes`；
- 原始题注块保留在内部块输出中供审计，但**不得在下游有序内容中重复出现
  一次**；
- 解析器内嵌题注（image_caption/table_caption）与邻域分离题注
  （距离阈值 + 题注编号模式）都要关联。

#### 5.7 视觉资源保留

- 输入中有效的图片文件**不得修改**；
- 输出必须说明来源：`parsed_asset`（解析 JSON 中的路径）或 `pdf_crop`
  （原 PDF page+bbox 补裁）；
- 33 个无效表格路径（`"images/"`）必须补裁；不得把目录路径当作有效图片路径。

#### 5.8 自动验证

按第 8 节验收标准逐条自动验证，结果写入报告；verify 阶段对当前输出产物
执行结构校验 + 资源校验 + 与全新重跑结果逐一哈希比对。

### 6. 删除的冗余功能（v1 → v2）

从任务书、程序入口、配置、README、测试和验收标准中删除：

1. OCR 数值人工修正工作流及 `ocr_corrections_manual.json` 驱动的内容改写；
2. OCR 修正截图和人工 confirmed/rejected 状态；
3. `render_verification_images.py` 对 OCR 修正的核验流程；
4. `verify_samples.py` 的大规模标题/正文截图目检流程；
5. `visual_sampling.md` 作为验收门禁；
6. 表格 rowspan/colspan 展开；
7. 多级表头恢复；
8. 表格数值和单位拆分；
9. 表格异常值、异常区间、缺失单元格推测；
10. 表格行级结构规范化与 `normalized_tables.jsonl` 作为必需输出；
11. 基于 OCR 表头或单元格语义的跨页表格确认；
12. "所有数值修正必须人工目检"这一验收条件。

保留的通用能力：JSON 加载、标题恢复、阅读顺序排序、溯源注册、图题/表题
关联、PDF bbox 裁剪。

### 7. 明确禁止

- 使用 LLM 或规则理解图片语义；
- 使用 VLM 抽取图表内容；
- 根据专业知识修改 OCR 文本；
- 推测表格缺失值；
- 生成实体、关系或工艺规则；
- 生成焊接参数推荐；
- 将 OCR HTML 当作表格视觉事实；
- 打乱图片、表格与文本的原始顺序；
- 丢失融合前的物理来源。

### 8. 验收标准（全部自动验证）

1. 所有 section_id 唯一，父子关系无循环、无孤立节点。
2. 所有章节的 start/end 范围合理：end 结束于下一个同级或更高级标题之前，
   不得全部结束于最后一页。
3. 每个正文、图片和表格恰好归属一个最深层 section_id。
4. 每个章节 content 的 order 严格递增。
5. 递归遍历所有章节后，内容顺序与原始 JSON 阅读顺序一致。
6. 段落融合不得跨越标题、图片或表格。
7. 融合文本能通过 member_block_ids 完整还原到原始块。
8. 741 个 image 块均有真实存在的图片文件。
9. 193 个 table 块均有真实存在的图片文件。
10. 不允许 asset_path 是目录。
11. 33 个缺失表格图片必须由 PDF bbox 补裁，并标记 asset_origin=pdf_crop。
12. 所有 figure/table 的 source_ref 都能返回 pdf_page、bbox 和 asset_path。
13. 图题、表题不会在面向下游的 content 中重复计数。
14. raw_text 不被覆盖。
15. 输出不包含实体、关系、工艺规则或参数推荐字段。
16. 相同输入连续运行两次，输出哈希一致。
17. 验收不因 OCR 数值人工确认或图片语义目检而阻塞。

视觉抽样（可选，不作验收门禁）只用于检查：标题边界、章节归属、内容顺序、
图片/表格路径与 bbox、题注关联；不要求理解图片或表格内容。

### 9. 实现要求

- 主流程单一命令可执行：`python preprocess_document.py run`；
- 审计模式：`python preprocess_document.py audit`（只读）；
- 验证模式：`python preprocess_document.py validate`（对当前输出做结构 +
  资源校验，并与两次全新重跑逐一哈希比对）；
- 配置、代码、规则分离；每阶段独立日志；中途失败不得产生看似完整的最终
  结果；输出 UTF-8；不修改输入文件；
- 任何输出结构或规则变更必须更新版本号（`preprocess_version`）、README 与
  回归测试；
- 每条核心验收规则必须有对应回归测试。

### 10. 完成汇报格式

```markdown
## 完成情况
- 已完成模块：
- 未完成模块：
- 阻塞项：

## 输入与输出
- 输入文件及版本：
- 输出文件：
- 运行命令：

## 数据统计
- 页面数：
- 标题数 / 章节数：
- 文本段数：
- 图片数 / 表格数：
- PDF 补裁表格图片数：

## 验证结果
- 测试结果：
- 自动验收：
- 重复运行一致性：
- 资源完整性：

## 风险与后续建议
- 尚未解决的标题或顺序问题：
- 是否可以交给后续 VLM：
```

### 11. 最终边界声明

本任务的终点是：**结构正确、顺序可恢复、视觉资源可用、位置可追溯的
结构化文档数据**，供文本抽取与 VLM 直接消费。

本任务的终点不是：理解图片/表格内容、生成实体/关系/工艺规则/参数推荐。
图片与表格的语义理解由后续 VLM 阶段完成。
