# welding_kg.docprep — GMAW 文档结构化预处理模块

把《气体保护焊工艺基础及应用》（殷树言 编著）的 OCR 解析结果转换为
**按标题组织、保持原始阅读顺序、可供文本抽取和 VLM 处理的结构化文档**。
本模块是 GMAWGraph 的文档处理与图谱构建前置模块。

> 工作规则见 [CLAUDE.md](CLAUDE.md)；任务书见 [gmaw_document_preprocessing_task.md](gmaw_document_preprocessing_task.md)。
> 版本历史：v2.0.0 独立项目（DocProduce）→ v2.1.0 融合进 GMAWGraph
> （路径与包名变更，输出结构不变）。

## 1. 定位与边界

- 本模块**不理解**图片和表格中的工艺知识；图片与表格的语义理解由
  **后续 VLM** 完成。
- 主输出：`data/docprep/document_structure.json`（下游接口）。
- `data/docprep/normalized_blocks.jsonl` 仅为内部中间结果，**不是主接口**。
- 明确不做：OCR 数值人工修正、表格 rowspan/colspan 展开、多级表头、
  数值单位拆分、表格异常推测、跨页表格语义确认、视觉抽样验收门禁。

## 2. 运行（GMAWGraph conda 环境）

```bash
conda activate /ENV/Anaconda/envs/jm/GMAWGraph
cd /CODE/jm/0Project_crp/GMAWGraph

python scripts/preprocess_document.py audit     # 只读审计（统计 + 资源分类）
python scripts/preprocess_document.py run       # 完整结构化预处理（生成 pdf_crop）
python scripts/preprocess_document.py validate  # 当前输出校验 + 资源校验 + 确定性
python -m pytest -q tests/test_docprep.py       # 回归测试（17 条验收标准）
python tests/check_docprep_assets.py            # 独立资源完整性验证
```

依赖：Python 3.11（conda 环境）、PyYAML、poppler-utils（pdfinfo/pdftoppm）。
配置：`config/docprep.yaml`（相对路径基于 `config/` 解析）。

输入（须存在，缺失即阻塞）：

| 输入 | 路径 |
|---|---|
| 解析 JSON | `../GMAW/hybrid_ocr/GMAW(OCR)_content_list_v2.json` |
| 原始 PDF | `../GMAW/hybrid_ocr/GMAW(OCR)_origin.pdf` |
| 解析器图片 | `../GMAW/hybrid_ocr/images/` |

## 3. 处理链路

```text
OCR JSON → 内容块审计 → 阅读顺序恢复 → 标题识别和层级恢复
→ 每块绑定 section_id → 连续文本块融合为 text_segment
→ 图片/表格与题注关联 → 视觉资源解析与 PDF 补裁
→ 生成"标题—有序内容"结构 → 自动验证（溯源/顺序/资源/确定性）
```

## 4. 输出文件（data/docprep/）

| 文件 | 内容 |
|---|---|
| `document_structure.json` | **主输出**：sections（含 content 有序流）+ section_order |
| `source_registry.json` | source_ref → pdf_page/bbox；figure/table 项含 asset_path |
| `preprocessing_report.json` | 输入统计、章节/文本段/资源统计、pdf_crop 统计、待复核问题、自动验收结果 |
| `normalized_blocks.jsonl` | 内部中间结果（审计用） |
| `assets/pdf_crop/` | 33 张补裁表格图片（PNG） |

### 4.1 document_structure.json（下游接口）

```json
{
  "meta": {"document_id": "GMAW", "document_version": "v2",
           "preprocess_version": "2.1.0"},
  "sections": [
    {
      "section_id": "gmaw:v2:sec:2.1.1",
      "title": "2.1.1 气体放电的基本概念",
      "level": 3,
      "parent_section_id": "gmaw:v2:sec:2.1",
      "heading_path": ["第2章 焊接电弧", "2.1 电弧的物理基础", "2.1.1 气体放电的基本概念"],
      "start_source_ref": "...", "end_source_ref": "...",
      "start_page": 17, "end_page": 18,
      "content": [
        {"item_type": "text_segment", "item_id": "gmaw:v2:item:seg0028",
         "order": 1, "text": "...", "member_block_ids": ["..."],
         "source_refs": ["..."], "sources": [{"pdf_page": 17, "bbox": [..], "source_ref": ".."}]},
        {"item_type": "figure", "item_id": "gmaw:v2:item:fig0029", "order": 2,
         "asset_path": "../GMAW/hybrid_ocr/images/xxx.jpg",
         "original_asset_path": "images/xxx.jpg",
         "asset_origin": "parsed_asset", "asset_exists": true,
         "sha256": "...", "mime_type": "image/jpeg",
         "caption": "图2-1 直流放电电路", "footnotes": [],
         "pdf_page": 17, "bbox": [...], "source_ref": "..."},
        {"item_type": "subsection", "item_id": "gmaw:v2:sec:2.1.1.1", "order": 3,
         "section_id": "gmaw:v2:sec:2.1.1.1"}
      ],
      "children": ["gmaw:v2:sec:2.1.1.1"]
    }
  ],
  "section_order": ["gmaw:v2:sec:root", "..."]
}
```

### 4.2 下游消费方式

**文本抽取**：

1. 从 `section_order` 首位（根节点）开始递归遍历；
2. 对每个 section 的 `content` 按 `order` 读取：
   - `text_segment` → `text`（连续融合文本，`member_block_ids`/`sources`
     可回溯到原始块）；
   - `subsection` → 递归进入 `section_id` 对应 section；
   - `figure`/`table` → 视觉项（见下）。

**VLM 处理视觉项**：

1. 读取 `figure`/`table` 项的 `asset_path` 加载图片；
2. `caption` 是图题/表题，`footnotes` 是脚注，可直接作为提示词上下文；
3. `ocr_html`（仅 table）为解析器 OCR 的辅助文本，**不是原图替代品**；
4. `pdf_page` + `bbox` + `source_ref` 用于回原 PDF 溯源。

**图谱构建衔接**：Neo4j 只保存外部引用 `source_refs`（见 GMAWGraph 主
README）。docprep 的 `source_registry.json` 提供 source_ref → 页码/bbox
映射，图谱中的引用可经此回溯到 PDF 原文位置。

### 4.3 视觉资源模型

| 字段 | 说明 |
|---|---|
| `asset_path` | 可用路径（相对 GMAWGraph 根目录；禁止是目录） |
| `original_asset_path` | 解析 JSON 中记录的原始路径 |
| `asset_origin` | `parsed_asset`（解析器图片，原样使用）或 `pdf_crop`（原 PDF page+bbox 补裁） |
| `asset_exists` / `sha256` / `mime_type` | 存在性、内容哈希、MIME |

已知事实（程序自检）：741 个 image 块均有有效图片；193 个 table 块中
33 个的 `image_source.path` 是目录 `"images/"`，由程序按原 PDF 补裁为
`data/docprep/assets/pdf_crop/*.png`（33 张）。输入中的有效图片
**不修改、不复制**。

## 5. 数据统计（2026-08-16 运行，v2.1.0）

- 页面 471；章节 427（含根；层级 1:20 / 2:31 / 3:85 / 4:189 / 5:92 / 6:9）
- 文本段 788；图片项 741；表格项 193；subsection 引用 426
- 资源：parsed_asset 901（741 图 + 160 表）、pdf_crop 33、失败 0
- 待复核：标题边界 1 处（p141，已标 needs_review）、标题正文粘连段落
  84 处、无题注图 114 / 无表题表 37

## 6. 验证结果

```text
回归测试         11 个测试全部通过（17 条验收标准 + 历史 bug 回归）
validate         当前输出结构校验 ✓ / 资源校验 934 项 ✓ /
                 两次重跑哈希一致 ✓ / 当前输出与重跑一致 ✓
资源完整性       1838 项检查全部通过
```
