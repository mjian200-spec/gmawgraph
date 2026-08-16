# welding_kg.docprep 工作规则（给本目录内所有编程智能体）

本目录（`src/welding_kg/docprep/`）实现《GMAW 文档结构化预处理任务书（v2）》，
是 GMAWGraph 的文档处理与图谱构建前置模块。**工作前必须完整阅读：**
`../../../docs/requirements/archive/REQ-001-document-preprocessing.md`（已完成的验收依据）、
`README.md`（下游用法）、
GMAWGraph 根目录 README 与 tests/README.md（项目约定）。

## 铁律 1：结论必须由证据计算，禁止声明

- 置信度、验收标准、统计数字必须由数据/证据计算得出，禁止字面量
  `True`/`False` 或写死数值。
- 验收标准（`validate.py`）只从检查注册表 `checks_by_key` 派生；有静态
  测试守护（`tests/test_docprep.py::test_static_guards`）。
- 汇报必须给出统计数据和对应输出证据，不得仅汇报"测试通过"。

## 铁律 2：以最终消费接口反推设计

- 下游接口是 `data/docprep/document_structure.json`：文本抽取读
  `sections[].content` 的 `text_segment`；VLM 读 `figure/table` 的
  `asset_path` + `caption`；图谱构建经 `source_registry.json` 回溯证据。
- 任何新功能先问：下游怎么消费它？接口字段先定（任务书第 4 节），
  再写实现与验收测试。
- 不得把内部中间结果（normalized_blocks.jsonl）当主接口。

## 铁律 3：视觉资源是硬约束

- 所有 figure/table 必须有真实存在的图片文件；`asset_path` 禁止是目录；
  输入中无效的表格图片路径（`"images/"`）必须按原 PDF page+bbox 补裁，
  标记 `asset_origin=pdf_crop`。
- 输入中的有效图片**不得修改**（不复制、不改名、不重编码）。
- 任何视觉资源变更都必须过 `tests/check_docprep_assets.py`。

## 铁律 4：本阶段不理解内容

- 禁止理解图片/表格工艺内容（那是 VLM 的活）；
- 禁止根据专业知识修改 OCR 文本、推测表格缺失值、生成实体/关系/规则；
- `ocr_html` 只是辅助字段，不是原图替代品；
- 融合文本禁止为了"通顺"生成、改写或补充内容；`raw_text` 永不覆盖。

## 铁律 5：validate 的对象是当前产物

- `validate` 必须：①对**当前 `data/docprep/`** 做结构校验（validate_outputs）；
  ②资源校验（asset 文件存在/可读/哈希一致）；③当前 `data/docprep/` 与两次全新
  重跑逐文件哈希比对。三者都过才算通过。

## 铁律 6：依赖缺失必须阻塞

- 解析 JSON 或原始 PDF 缺失 → `PipelineError` 停止并报告阻塞，
  不允许降级后产出"看似完整"的结果。

## 铁律 7：版本纪律 + 回归测试

- 任何输出结构/规则变更：递增 `src/welding_kg/docprep/__init__.py` 的
  `PREPROCESS_VERSION`，同步 `config/docprep.yaml` 与 `config.py`
  默认值，更新 README，为每条核心规则写回归测试。
- 每条任务书验收标准（第 8 节 17 条）必须有对应测试。
- 改动先写失败测试，再实现修复。运行：
  ```bash
  /ENV/Anaconda/envs/jm/GMAWGraph/bin/python -m compileall src/welding_kg/docprep
  /ENV/Anaconda/envs/jm/GMAWGraph/bin/python -m pytest -q tests/test_docprep.py
  /ENV/Anaconda/envs/jm/GMAWGraph/bin/python tests/check_docprep_assets.py
  /ENV/Anaconda/envs/jm/GMAWGraph/bin/python scripts/preprocess_document.py run
  /ENV/Anaconda/envs/jm/GMAWGraph/bin/python scripts/preprocess_document.py validate
  ```

## 数据速查

- 输入：`../GMAW/hybrid_ocr/GMAW(OCR)_content_list_v2.json`（471 页，相对
  `config/` 解析）＋原 PDF；741 个 image 块均有有效图片；193 个 table 块中
  33 个 `image_source.path` 为 `"images/"`（目录）需 pdf_crop。
- conda 环境：`/ENV/Anaconda/envs/jm/GMAWGraph`（Python 3.11），
  所有运行/测试命令必须用该环境的 python。
- GMAWGraph 是 Git 仓库：改动前 git status/git diff；DocProduce 旧目录
  已废弃（无 Git 元数据），勿再改。
- bbox 是解析器扫描图坐标系（约 1000×1040），转 PDF pt 经验映射
  x×0.7485、y×1.0（assets.py 已内置）。
- 旋转版式页：表格 x0 < 100。
- 输出目录 `data/docprep/` 为生成物（.gitignore 已忽略），不提交。

## 已知失败模式清单（错误归因，勿重犯）

| 归因 | 表现 | 根本解 |
|---|---|---|
| 结论与证据脱钩 | 置信度写死、验收硬编码、文本层冒充图像核验 | 铁律 1 |
| 引用以不稳定坐标为键 | block_id 漂移、行 bbox 继承首页 | 引用必须可解析回写（v2 已删除修正工作流，此模式仍适用于任何引用设计） |
| 验证对象错误 | validate 不检查当前输出 | 铁律 5 |
| 需求边界执行不严 | 缺 PDF 只警告 | 铁律 6 |
| 版本管理缺失 | 程序改三轮版本号不变 | 铁律 7 |
| 任务书写了功能但没有下游接口 | v1 的表格规范化/OCR 修正无人消费 | 铁律 2 |
