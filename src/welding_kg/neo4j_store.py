"""Neo4j 连接与模式管理（规范 §9 数据库层、§11 实现规则）。"""

from __future__ import annotations

from neo4j import GraphDatabase, NotificationClassification

from .settings import Settings, load_schema_config


class Neo4jStore:
    """Neo4j 存取类。

    通用 Cypher 执行函数只供内部模块使用，不暴露给 LLM；
    动态标签和关系必须先映射到 schema.yaml 白名单（规范 §11）。
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        # 建立驱动；连接惰性建立，首次执行时才会真正握手。
        # 屏蔽 UNRECOGNIZED 类通知（如首次写入前属性不存在的 01N42 提示），
        # 避免批量导入时输出刷屏，同时保留 HINT/PERFORMANCE 等有用提示。
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            notifications_disabled_classifications=[
                NotificationClassification.UNRECOGNIZED
            ],
        )

    def verify_connectivity(self) -> None:
        """验证数据库连通性；失败时抛出带上下文的 RuntimeError。"""
        try:
            self._driver.verify_connectivity()
        except Exception as exc:  # noqa: BLE001 需统一转为可读错误
            raise RuntimeError(f"Neo4j 连接失败（{self._settings.neo4j_uri}）：{exc}") from exc

    def initialize_schema(self) -> None:
        """初始化六类唯一约束与必要索引，可重复执行（规范 §11）。"""
        schema = load_schema_config()
        node_types = schema["node_types"]

        # 查询已存在的约束/索引名，避免重复创建
        existing_constraints = {
            row["name"]
            for row in self.execute_read("SHOW CONSTRAINTS YIELD name RETURN name", {})
        }
        existing_indexes = {
            row["name"] for row in self.execute_read("SHOW INDEXES YIELD name RETURN name", {})
        }

        # 1) 六类节点唯一约束：name 前缀保证可读
        for label, spec in node_types.items():
            key = spec["unique_key"]
            constraint_name = f"unique_{label.lower()}_{key}"
            if constraint_name not in existing_constraints:
                query = (
                    f"CREATE CONSTRAINT {constraint_name} "
                    f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE"
                )
                self.execute_write(query, {})

        # 2) Case 检索字段索引（先按 process 过滤、再排序，规范 §10）
        retrieval_index_fields = [
            "process",
            "material",
            "thickness_mm",
            "joint_type",
            "position",
            "wire_diameter_mm",
            "shielding_gas",
        ]
        for field in retrieval_index_fields:
            index_name = f"index_case_{field}"
            if index_name not in existing_indexes:
                self.execute_write(
                    f"CREATE INDEX {index_name} FOR (n:Case) ON (n.{field})", {}
                )

    def execute_read(self, query: str, params: dict) -> list[dict]:
        """执行只读 Cypher，返回记录字典列表。"""
        with self._driver.session() as session:
            result = session.run(query, params)
            return [dict(record) for record in result]

    def execute_write(self, query: str, params: dict) -> list[dict]:
        """执行写 Cypher（隐式事务），返回记录字典列表。"""
        with self._driver.session() as session:
            result = session.run(query, params)
            return [dict(record) for record in result]

    def execute_write_tx(self, work) -> object:
        """在单个显式事务中执行写入回调（规范 §11：每个输入文件一个事务）。"""
        with self._driver.session() as session:
            return session.execute_write(work)

    def close(self) -> None:
        """关闭驱动，释放连接资源。"""
        self._driver.close()
