"""welding_kg.docprep — GMAW 文档结构化预处理模块（任务书 v2）。

作为 GMAWGraph 的文档处理与图谱构建前置模块：
- 主输出 document_structure.json 供文本抽取与后续 VLM 处理；
- source_registry.json 的 source_ref 编码与图谱 source_refs 引用约定一致，
  Neo4j 只保存外部引用，证据原文通过 source_ref 回溯到 PDF 页码/bbox。

处理链路：
审计 → 标准化 → 页码/顺序 → 标题/章节树 → 清洗 → 资源解析/补裁
→ 题注关联 → 有序内容与文本融合 → 自动验证 → 输出 document_structure.json

版本纪律：任何会改变输出结构/内容的程序改动必须递增 PREPROCESS_VERSION。
"""

__version__ = "2.1.0"
PREPROCESS_VERSION = __version__
