# 测试说明

测试分两组（验收规范 P1），全部使用项目 Conda 环境：

```bash
/ENV/Anaconda/envs/jm/GMAWGraph/bin/python
```

## 单元测试（无外部服务，必须全部通过）

```bash
cd /CODE/jm/0Project_crp/GMAWGraph
/ENV/Anaconda/envs/jm/GMAWGraph/bin/python -m pytest -q -m "not integration"
```

- `test_adjustment_unit.py`：纯函数 + FakeStore 端到端 + LLM 桩。
  覆盖量化、基准案例选择、幅度估计、大小标签、方向冲突、范围核验、
  模式约束、置信度加权和、配置校验、模型校验、完整知识路径、
  部分缺步长、非本阶段参数、未知范围降分、数量上限、LLM 越界路径
  拒绝与数值不可变性、两次运行确定性。
- 不依赖 Neo4j / BGE / LLM 服务；嵌入服务不可用时按既有降级路径
  （纯结构化检索）运行。
- `helpers.py` 中的图谱行与案例全部为 `SYNTHETIC` 工程测试数据，
  不来自书籍或真实工艺案例。

## 集成测试（强制真实 Neo4j，正式验收必须执行）

```bash
# 先启动服务并导入数据
/ENV/Anaconda/envs/jm/GMAWGraph/bin/python scripts/init_graph.py
/ENV/Anaconda/envs/jm/GMAWGraph/bin/python scripts/import_graph.py data/seed/graph_seed.json
/ENV/Anaconda/envs/jm/GMAWGraph/bin/python scripts/import_cases.py data/seed/cases.json

/ENV/Anaconda/envs/jm/GMAWGraph/bin/python -m pytest -q -m integration
```

- `test_adjustment_integration.py`：真实图谱步长、真实知识链独立性、
  并行确定性（两次）、端到端溯源与量化、路径 id 存在性、缺设备步长、
  一元模式电压、LLM 选择边界、最高分方案即首项。
- **Neo4j 不可达时直接失败，不跳过**——「测试被跳过」不等于「通过验收」。
- BGE/LLM 当前允许安全降级；强制在线调用断言属于后续优化项。
- 全部测试运行：`/ENV/Anaconda/envs/jm/GMAWGraph/bin/python -m pytest -q`
