"""运行配置（环境变量 + config YAML，规范 §12）。

数据库连接从环境变量读取，不提交真实密码；schema/retrieval 白名单与
权重保存在 config/*.yaml 中。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

# 项目根目录（src/welding_kg 的上一级目录再上一级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class Settings(BaseModel):
    """从环境变量加载的运行配置。

    默认值与 .env.example 保持一致；.env 文件位于项目根目录时自动加载。
    """

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "change_me"

    llm_base_url: str = "http://localhost:8000/v1"
    llm_model: str = "qwen3-32b"
    llm_api_key: str = "EMPTY"

    embedding_base_url: str = "http://localhost:8001/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_api_key: str = "EMPTY"

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构建配置；项目根目录存在 .env 时先加载它。"""
        load_dotenv(PROJECT_ROOT / ".env")
        return cls(
            neo4j_uri=os.getenv("NEO4J_URI", cls.model_fields["neo4j_uri"].default),
            neo4j_user=os.getenv("NEO4J_USER", cls.model_fields["neo4j_user"].default),
            neo4j_password=os.getenv("NEO4J_PASSWORD", cls.model_fields["neo4j_password"].default),
            llm_base_url=os.getenv("LLM_BASE_URL", cls.model_fields["llm_base_url"].default),
            llm_model=os.getenv("LLM_MODEL", cls.model_fields["llm_model"].default),
            llm_api_key=os.getenv("LLM_API_KEY", "EMPTY"),
            embedding_base_url=os.getenv("EMBEDDING_BASE_URL", cls.model_fields["embedding_base_url"].default),
            embedding_model=os.getenv("EMBEDDING_MODEL", cls.model_fields["embedding_model"].default),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", "EMPTY"),
        )


@lru_cache(maxsize=1)
def load_schema_config() -> dict:
    """加载 schema.yaml（节点/关系白名单与唯一键，规范 §4）。"""
    with open(CONFIG_DIR / "schema.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def load_retrieval_config() -> dict:
    """加载 retrieval.yaml（检索过滤、权重与 Top-K，规范 §10）。"""
    with open(CONFIG_DIR / "retrieval.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def load_adjustment_config() -> dict:
    """加载并校验 adjustment.yaml（修正量置信度权重与阈值，
    adjustment_generation_spec §13、验收规范 P1）。

    置信度权重必须非负且总和为 1，设备未知范围折算分必须在 [0, 1]；
    配置非法直接抛错，不静默回退默认值。
    """
    with open(CONFIG_DIR / "adjustment.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    weights = cfg.get("confidence_weights") or {}
    total = 0.0
    for key in ("similarity", "knowledge", "case_support", "consensus", "equipment"):
        value = weights.get(key)
        if value is None or float(value) < 0:
            raise ValueError(
                f"adjustment.yaml 的置信度权重缺失或为负：{key}={value!r}"
            )
        total += float(value)
    if abs(total - 1.0) > 1e-9:
        raise ValueError(
            f"adjustment.yaml 置信度权重之和必须为 1，当前为 {total}"
        )

    credit = float(cfg.get("equipment", {}).get("unknown_range_credit", 0.0))
    if not 0.0 <= credit <= 1.0:
        raise ValueError(
            f"adjustment.yaml equipment.unknown_range_credit 必须在 [0, 1]，当前为 {credit}"
        )
    return cfg
