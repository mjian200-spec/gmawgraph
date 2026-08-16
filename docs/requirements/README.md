# GMAWGraph 需求索引

## 当前阶段

正在进行术语发现、词义消歧和多层图谱实体规范化。Git 仓库中的
`docs/requirements/` 是需求与技术决策的唯一权威来源。

## 当前有效需求

| ID | 需求 | 状态 | 当前版本 |
|---|---|---|---|
| REQ-002 | 关键名词与实体规范化 | 已批准／待实施 | v1.0.0 |

## 已完成或废止需求

| ID | 需求 | 状态 | 当前版本 |
|---|---|---|---|
| REQ-000 | 焊接知识图谱最简原型 | 已完成 | v1.0.0 |
| REQ-001 | 文档结构化预处理 | 已完成 | v2.1.0 |

## 已确认技术路线

Qwen3.5-9B 全模态发现 → BGE-M3 语境召回 → Qwen3-32B 解释与消歧。
实体统一映射到工况、参数、机理、质量和设备五层图谱。

## 当前审核入口

审核 [REQ-002](active/REQ-002-entity-extraction.md)，并同时阅读
[ADR-001](decisions/ADR-001-graph-layers.md) 和
[ADR-002](decisions/ADR-002-model-pipeline.md)。智能体必须以
`manifest.yaml` 中的 `agent_read_order` 为准。

## 文档管理

- 小修改直接更新原文件，由 Git 保存差异；
- 目标、授权边界或数据契约发生实质变化时，升级任务书的 `version`；
- 需求被替代时，移入 `archive/`，将 `status` 设为 `superseded` 并填写
  `superseded_by`；
- 归档文档不得出现在 `active_requirements`；
- 每次状态或路径变更必须同步 `README.md`、`manifest.yaml` 和
  `changes/CHANGELOG.md`；
- 聊天记录不是需求依据，已确认结论必须进入任务书、ADR 或变更记录；
- 不使用“最终版”、“最新版”等文件名区分版本。
