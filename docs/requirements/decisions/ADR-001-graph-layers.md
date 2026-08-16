---
decision_id: ADR-001
title: 采用五层概念图谱
status: accepted
created_at: 2026-08-16
updated_at: 2026-08-16
related_requirements:
  - REQ-000
  - REQ-002
---

# ADR-001：采用五层概念图谱

## 状态

已确认。

## 决定

除案例层 `Case` 外，文档概念映射到五个图谱层：

- 工况层：`Condition`；
- 参数层：`Parameter`；
- 机理层：`Mechanism`；
- 质量层：`Quality`；
- 设备层：`Equipment`。

`Mechanism.layer` 只允许 `arc`、`transfer` 或 `pool`。节点标签、
唯一键和关系端点以 `config/schema.yaml` 为权威约束。

## 原因

分层保留了“工况→参数→机理→质量”的解释路径，并使设备能力可以
独立约束参数。这与现有案例检索和路径推理契约兼容。

## 被否决方案

- 将所有术语存入单一通用 `Entity` 标签；
- 在实体抽取阶段自行新增 `ProcessWindow` 或关系类型。

## 影响

实体规范化必须先确定词义，再分配标签与唯一键。本阶段不得修改
`config/schema.yaml` 的本体边界。
