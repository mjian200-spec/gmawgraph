"""外部 JSON 导入层（规范 §9 导入层、§6 外部 JSON 契约）。

导入顺序固定为：读取 → 模型校验 → 白名单校验 → 标准化 → 事务写入 → 报告。
节点与关系使用 MERGE，重复导入不得产生重复数据；
每个输入文件使用一个事务，非法引用导致整体回滚。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from .models import CasesFile, CaseRecord, GraphSeedFile, ImportResult
from .neo4j_store import Neo4jStore
from .settings import Settings, load_schema_config
from .case_retriever import embed_text

# 概念节点必须包含的属性（规范 §5：至少含 code/name、definition、source_refs；
# 设备节点以 equipment_id 替代 code）
_CONCEPT_LABELS = {"Condition", "Parameter", "Mechanism", "Quality", "Equipment"}


def _props_hash(props: dict) -> str:
    """计算属性字典的稳定指纹（用于判断导入内容是否变化）。"""
    dumped = json.dumps(props, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(dumped.encode("utf-8")).hexdigest()


def _normalize_props(props: dict, label: str, key_name: str) -> tuple[dict, list[str]]:
    """标准化节点属性，返回 (标准化属性, 错误列表)。

    规则：字符串去除首尾空白；source_refs 强制为字符串列表；
    概念节点校验 唯一键/name/definition/source_refs 齐备且唯一键非空。
    """
    errors: list[str] = []
    normalized: dict = {}
    for key, value in props.items():
        if isinstance(value, str):
            normalized[key] = value.strip()
        elif key == "source_refs":
            # 证据引用统一为字符串列表（规范 §5）
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                errors.append(f"source_refs 必须是字符串列表：{value!r}")
                continue
            normalized[key] = [x.strip() for x in value]
        else:
            normalized[key] = value

    if label in _CONCEPT_LABELS:
        # 概念节点至少含 唯一键/name、definition、source_refs（规范 §5）
        for required in (key_name, "name", "definition", "source_refs"):
            if required not in normalized:
                errors.append(f"{label} 节点缺少必填属性：{required}")
        key_value = normalized.get(key_name)
        if key_value is None or not str(key_value).strip():
            errors.append(f"{label} 节点的 {key_name} 不能为空")
    return normalized, errors


def _validate_seed(
    seed: GraphSeedFile, schema: dict
) -> tuple[list[dict], list[dict], list[str]]:
    """白名单校验并标准化种子文件，返回 (节点行, 关系行, 错误列表)。

    节点行 = {id, label, key, key_value, props, hash}；关系行 = {id, type,
    from_key..., to_key..., props, hash}。任何错误都会阻止写入（整体回滚）。
    """
    errors: list[str] = []
    node_types = schema["node_types"]
    rel_types = schema["relationship_types"]

    # ---- 节点校验 ----
    node_rows: list[dict] = []
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()  # (label, key_value) 防唯一键重复
    for node in seed.nodes:
        spec = node_types.get(node.label)
        if spec is None:
            errors.append(f"节点 {node.id!r} 的 label {node.label!r} 不在白名单中")
            continue
        if node.id in seen_ids:
            errors.append(f"节点 id 重复：{node.id!r}")
            continue
        seen_ids.add(node.id)

        props, prop_errors = _normalize_props(node.properties, node.label, spec["unique_key"])
        errors.extend(f"节点 {node.id!r}：{e}" for e in prop_errors)
        if prop_errors:
            continue

        # Mechanism 的 layer 必须属于 arc/transfer/pool（规范 §4）
        layer = props.get("layer")
        if node.label == "Mechanism" and layer is not None and layer not in spec["allowed_layers"]:
            errors.append(
                f"节点 {node.id!r} 的 Mechanism.layer={layer!r} 不在 "
                f"{spec['allowed_layers']} 中"
            )
            continue

        key = spec["unique_key"]
        key_value = props.get(key)
        if key_value is None:
            errors.append(f"节点 {node.id!r} 缺少唯一键 {key!r}")
            continue
        if (node.label, str(key_value)) in seen_keys:
            errors.append(f"唯一键重复：{node.label}.{key} = {key_value!r}")
            continue
        seen_keys.add((node.label, str(key_value)))

        props["id"] = node.id  # 文件内稳定引用一并保存
        props["props_hash"] = _props_hash(props)  # 指纹入库供幂等判断
        node_rows.append(
            {
                "id": node.id,
                "label": node.label,
                "key": key,
                "key_value": str(key_value),
                "props": props,
                "hash": props["props_hash"],
            }
        )

    # ---- 关系校验（依赖节点 id 解析）----
    id2row = {row["id"]: row for row in node_rows}
    rel_rows: list[dict] = []
    seen_rel_ids: set[str] = set()
    seen_rel_pairs: set[tuple[str, str, str]] = set()  # (from_id, type, to_id)
    for rel in seed.relationships:
        if rel.id in seen_rel_ids:
            errors.append(f"关系 id 重复：{rel.id!r}")
            continue
        seen_rel_ids.add(rel.id)
        allowed_pairs = rel_types.get(rel.type)
        if allowed_pairs is None:
            errors.append(f"关系 {rel.id!r} 的类型 {rel.type!r} 不在白名单中")
            continue
        from_row = id2row.get(rel.from_)
        to_row = id2row.get(rel.to)
        if from_row is None or to_row is None:
            errors.append(f"关系 {rel.id!r} 的端点 {rel.from_!r}/{rel.to!r} 不在节点列表中")
            continue
        if [from_row["label"], to_row["label"]] not in allowed_pairs:
            errors.append(
                f"关系 {rel.id!r}：{rel.type} 不允许从 {from_row['label']} 指向 {to_row['label']}"
            )
            continue
        if (rel.from_, rel.type, rel.to) in seen_rel_pairs:
            errors.append(f"关系重复：{rel.from_} -[{rel.type}]-> {rel.to}")
            continue
        seen_rel_pairs.add((rel.from_, rel.type, rel.to))

        # 知识关系属性统一标准化（规范 §5）
        props: dict = {}
        for key, value in rel.properties.items():
            if isinstance(value, str):
                props[key] = value.strip()
            elif key == "source_refs":
                if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                    errors.append(f"关系 {rel.id!r} 的 source_refs 必须是字符串列表")
                    continue
                props[key] = [x.strip() for x in value]
            else:
                props[key] = value
        if "source_refs" not in props:
            props["source_refs"] = []
        props["id"] = rel.id
        props["props_hash"] = _props_hash(props)  # 指纹入库供幂等判断
        rel_rows.append(
            {
                "id": rel.id,
                "type": rel.type,
                "from": from_row,
                "to": to_row,
                "props": props,
                "hash": props["props_hash"],
            }
        )
    return node_rows, rel_rows, errors


def _write_nodes(tx, node_rows: list[dict]) -> dict:
    """在事务内按 创建/更新/跳过 分类写入节点，返回计数。

    先查询既有 (唯一键 → 指纹)，指纹相同视为跳过（不写库），
    不同视为更新，不存在视为创建（规范：MERGE 语义 + 精确计数）。
    """
    counts = {"created": 0, "updated": 0, "skipped": 0}
    # 按 label 分组预查既有指纹
    by_label: dict[str, list[dict]] = {}
    for row in node_rows:
        by_label.setdefault(row["label"], []).append(row)

    for label, rows in by_label.items():
        key = rows[0]["key"]
        keys = [row["key_value"] for row in rows]
        match_query = f"MATCH (n:{label}) WHERE n.{key} IN $keys RETURN n.{key} AS k, n.props_hash AS h"
        result = tx.run(match_query, {"keys": keys})
        existing = {record["k"]: record.get("h") for record in result}

        to_write = []
        for row in rows:
            old_hash = existing.get(row["key_value"])
            if old_hash is None:
                row["action"] = "create"
                counts["created"] += 1
                to_write.append(row)
            elif old_hash != row["hash"]:
                row["action"] = "update"
                counts["updated"] += 1
                to_write.append(row)
            else:
                row["action"] = "skip"
                counts["skipped"] += 1

        merge_query = (
            f"MERGE (n:{label} {{{key}: $kv}}) "
            "ON CREATE SET n = $props ON MATCH SET n = $props"
        )
        for row in to_write:
            tx.run(merge_query, {"kv": row["key_value"], "props": row["props"]})
    return counts


def _write_relationships(tx, rel_rows: list[dict]) -> dict:
    """在事务内按 创建/更新/跳过 分类写入关系，返回计数（语义同节点）。"""
    counts = {"created": 0, "updated": 0, "skipped": 0}
    for row in rel_rows:
        f, t = row["from"], row["to"]  # noqa: E741 局部变量名保持短小
        from_label, from_key = f["label"], f["key"]
        to_label, to_key = t["label"], t["key"]
        rel_type = row["type"]
        # 预查既有关系指纹：MERGE 语义下 (起点,类型,终点) 唯一
        probe_query = (
            f"MATCH (a:{from_label} {{{from_key}: $fk}})"
            f"-[r:{rel_type}]->"
            f"(b:{to_label} {{{to_key}: $tk}}) "
            "RETURN r.props_hash AS h"
        )
        result = tx.run(
            probe_query, {"fk": f["key_value"], "tk": t["key_value"]}
        )
        old_hash = next(iter(result), None)
        old_hash = old_hash.get("h") if old_hash else None
        if old_hash is None:
            counts["created"] += 1
        elif old_hash != row["hash"]:
            counts["updated"] += 1
        else:
            counts["skipped"] += 1
            continue
        write_query = (
            f"MATCH (a:{from_label} {{{from_key}: $fk}}),"
            f"(b:{to_label} {{{to_key}: $tk}}) "
            f"MERGE (a)-[r:{rel_type}]->(b) SET r = $props"
        )
        tx.run(
            write_query,
            {"fk": f["key_value"], "tk": t["key_value"], "props": row["props"]},
        )
    return counts


def import_graph_json(path: str, store: Neo4jStore) -> ImportResult:
    """导入图谱规则 JSON（graph_seed.json，规范 §6）。

    先通过 Pydantic 与白名单校验再开启写事务；任何校验错误都不写库。
    """
    file_path = Path(path)
    result = ImportResult(file=str(file_path))
    schema = load_schema_config()

    # 1) 读取
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.errors.append(f"读取失败：{exc}")
        return result

    # 2) Pydantic 模型校验
    try:
        seed = GraphSeedFile.model_validate(raw)
    except ValidationError as exc:
        result.errors.append(f"模型校验失败：{exc}")
        return result

    # 3) 白名单校验 + 4) 标准化
    node_rows, rel_rows, errors = _validate_seed(seed, schema)
    if errors:
        result.errors.extend(errors)
        return result  # 整体回滚语义：不开启任何写事务

    # 5) 单事务写入
    def work(tx) -> dict:
        node_counts = _write_nodes(tx, node_rows)
        rel_counts = _write_relationships(tx, rel_rows)
        # 节点与关系计数求和（同名字段不能直接合并）
        return {
            "created": node_counts["created"] + rel_counts["created"],
            "updated": node_counts["updated"] + rel_counts["updated"],
            "skipped": node_counts["skipped"] + rel_counts["skipped"],
        }

    try:
        counts = store.execute_write_tx(work)
    except Exception as exc:  # noqa: BLE001 任何写库异常都反映到报告
        result.errors.append(f"事务写入失败：{exc}")
        return result

    result.created = counts["created"]
    result.updated = counts["updated"]
    result.skipped = counts["skipped"]
    return result


def import_case_json(path: str, store: Neo4jStore) -> ImportResult:
    """导入案例 JSON（cases.json，规范 §6）。

    引用（conditions/parameters/results）必须在数据库中已存在，否则整体回滚；
    导入时经 BGE-M3 计算 retrieval_text 的嵌入向量，服务不可用时降级并告警。
    """
    file_path = Path(path)
    result = ImportResult(file=str(file_path))
    settings = Settings.from_env()

    # 1) 读取 + 2) Pydantic 校验
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
        cases_file = CasesFile.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        result.errors.append(f"读取或模型校验失败：{exc}")
        return result

    # 3) 文件内校验：case_id 不重复
    cases: list[CaseRecord] = cases_file.cases
    seen_ids: set[str] = set()
    for case in cases:
        if case.case_id in seen_ids:
            result.errors.append(f"case_id 重复：{case.case_id!r}")
        seen_ids.add(case.case_id)
    if result.errors:
        return result

    # 4) 引用校验：引用格式为 {label_lower}:{key_value}（规范 §6），
    #    所有引用节点必须在图中存在，否则整体回滚
    ref_groups = [
        ("conditions", "Condition", "code"),
        ("parameters", "Parameter", "code"),
        ("results", "Quality", "code"),
    ]
    for field, label, key in ref_groups:
        refs = {ref for case in cases for ref in getattr(case, field)}
        # 格式校验 + 拆出唯一键值（"condition:flat_position" → "flat_position"）
        malformed = [ref for ref in refs if ":" not in ref]
        if malformed:
            result.errors.append(f"{field} 引用格式错误（应为 label:key）：{sorted(malformed)[:10]}")
            continue
        codes = {ref.split(":", 1)[1] for ref in refs}
        if not codes:
            continue
        rows = store.execute_read(
            f"MATCH (n:{label}) WHERE n.{key} IN $codes RETURN n.{key} AS k",
            {"codes": sorted(codes)},
        )
        existing = {row["k"] for row in rows}
        missing = codes - existing
        if missing:
            result.errors.append(
                f"{field} 引用不存在的 {label} 节点：{sorted(missing)[:20]}"
                f"{'…' if len(missing) > 20 else ''}"
            )
    if result.errors:
        return result  # 非法引用导致整体回滚

    # 5) 嵌入计算（失败不阻断导入，只告警）
    rows: list[dict] = []
    for case in cases:
        embedding = None
        if case.retrieval_text:
            embedding = embed_text(case.retrieval_text, settings)
            if embedding is None:
                result.warnings.append(f"嵌入服务不可用，案例 {case.case_id} 未写入 embedding")
        props = case.model_dump(exclude={"conditions", "parameters", "results"})
        if embedding is not None:
            props["embedding"] = embedding  # 嵌入向量写入属性
        elif props.get("embedding") is None:
            # embedding 为 None 时不写入该键，避免 ON MATCH SET 覆盖删除既有向量
            props.pop("embedding", None)
        props["props_hash"] = _props_hash(props)
        props["id"] = case.case_id
        rows.append({"case": case, "props": props})

    # 6) 单事务写入（Case 节点 + HAS_* 关系）
    def work(tx) -> dict:
        counts = {"created": 0, "updated": 0, "skipped": 0}
        # 预查既有案例指纹用于精确计数
        ids = [row["case"].case_id for row in rows]
        existing = {
            record["k"]: record.get("h")
            for record in tx.run(
                "MATCH (n:Case) WHERE n.case_id IN $ids RETURN n.case_id AS k, n.props_hash AS h",
                {"ids": ids},
            )
        }
        for row in rows:
            case = row["case"]
            old_hash = existing.get(case.case_id)
            if old_hash is None:
                counts["created"] += 1
                to_write = True
            elif old_hash != row["props"]["props_hash"]:
                counts["updated"] += 1
                to_write = True
            else:
                counts["skipped"] += 1
                to_write = False
            if to_write:
                tx.run(
                    "MERGE (n:Case {case_id: $cid}) "
                    "ON CREATE SET n = $props ON MATCH SET n = $props",
                    {"cid": case.case_id, "props": row["props"]},
                )
            # 引用关系：HAS_CONDITION / HAS_PARAMETER / HAS_RESULT（规范 §4）
            for ref_field, rel_type, label, key in [
                ("conditions", "HAS_CONDITION", "Condition", "code"),
                ("parameters", "HAS_PARAMETER", "Parameter", "code"),
                ("results", "HAS_RESULT", "Quality", "code"),
            ]:
                for ref in getattr(case, ref_field):
                    # 引用格式为 {label_lower}:{key_value}，如 condition:flat_position
                    key_value = ref.split(":", 1)[1]
                    tx.run(
                        f"MATCH (n:Case {{case_id: $cid}}), (x:{label} {{{key}: $kv}}) "
                        f"MERGE (n)-[:{rel_type}]->(x)",
                        {"cid": case.case_id, "kv": key_value},
                    )
        return counts

    try:
        counts = store.execute_write_tx(work)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"事务写入失败：{exc}")
        return result

    result.created = counts["created"]
    result.updated = counts["updated"]
    result.skipped = counts["skipped"]
    return result
