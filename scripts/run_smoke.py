#!/usr/bin/env python3
"""冒烟实验脚本（规范要求：重要功能需冒烟实验测试效率与准确率）。

使用 data/smoke/ 下的合成数据（SYNTHETIC，非书中知识）验证最小闭环：
  模式初始化 → 图谱/案例导入幂等 → 案例检索（准确率+延迟）
  → 差异计算 → 多跳路径查询（正确性+延迟）→ LLM 路径选择
  → vLLM 服务基础吞吐

用法：python scripts/run_smoke.py
报告：data/smoke/report/smoke_report_<时间戳>.json
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openai import OpenAI  # noqa: E402

import httpx2  # noqa: E402  # 本地服务不走系统代理

from welding_kg.case_comparator import compare_case  # noqa: E402
from welding_kg.case_retriever import find_similar_cases  # noqa: E402
from welding_kg.graph_importer import import_case_json, import_graph_json  # noqa: E402
from welding_kg.models import WeldingRequirement  # noqa: E402
from welding_kg.neo4j_store import Neo4jStore  # noqa: E402
from welding_kg.path_planner import select_reasoning_paths  # noqa: E402
from welding_kg.path_retriever import query_reasoning_paths  # noqa: E402
from welding_kg.settings import Settings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SMOKE_DIR = PROJECT_ROOT / "data" / "smoke"
REPORT_DIR = SMOKE_DIR / "report"

# 冒烟实验统计项
RESULTS: list[dict] = []


def record(name: str, status: str, detail: str = "", metrics: dict | None = None) -> None:
    """记录一条冒烟实验结果（status: PASS / FAIL / WARN）。"""
    RESULTS.append({"name": name, "status": status, "detail": detail, "metrics": metrics or {}})
    mark = {"PASS": "✓", "FAIL": "✗", "WARN": "△"}[status]
    print(f"[{mark}] {name}  {detail}")


def main() -> int:
    """依次执行各冒烟阶段，输出汇总报告。"""
    settings = Settings.from_env()
    print(f"=== GMAWGraph 冒烟实验  {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print(f"Neo4j: {settings.neo4j_uri} | LLM: {settings.llm_base_url} "
          f"{settings.llm_model} | Embedding: {settings.embedding_base_url}\n")

    store = Neo4jStore(settings)
    try:
        # ---------- 阶段 0：清理合成数据（保证实验可重复） ----------
        # 只删除 SYNTHETIC 冒烟数据（图节点/案例），不触碰正式知识库数据
        deleted = store.execute_write(
            "MATCH (n) WHERE (n.case_id STARTS WITH 'case_smoke_') "
            "OR (n.source_refs IS NOT NULL AND "
            "    any(x IN n.source_refs WHERE x STARTS WITH 'SYNTHETIC')) "
            "DETACH DELETE n RETURN count(n) AS cnt",
            {},
        )
        if deleted:
            record("合成数据清理", "PASS", f"删除 {deleted[0]['cnt']} 个残留合成节点")
        else:
            record("合成数据清理", "PASS", "无残留合成数据")

        # ---------- 阶段 1：模式初始化幂等 ----------
        t0 = time.perf_counter()
        try:
            store.verify_connectivity()
            store.initialize_schema()
            store.initialize_schema()  # 重复执行验证幂等
            elapsed = time.perf_counter() - t0
            record("模式初始化幂等", "PASS", f"两次初始化无异常，耗时 {elapsed:.2f}s",
                   {"elapsed_s": round(elapsed, 3)})
        except Exception as exc:  # noqa: BLE001
            record("模式初始化幂等", "FAIL", str(exc))
            return 1

        # ---------- 阶段 2：图谱导入幂等 ----------
        seed_file = SMOKE_DIR / "graph_seed_smoke.json"
        t0 = time.perf_counter()
        first = import_graph_json(str(seed_file), store)
        elapsed_first = time.perf_counter() - t0
        t0 = time.perf_counter()
        second = import_graph_json(str(seed_file), store)
        elapsed_second = time.perf_counter() - t0
        if first.errors:
            record("图谱导入", "FAIL", "; ".join(first.errors))
            return 1
        if second.created == 0 and second.errors == []:
            record(
                "图谱导入幂等",
                "PASS",
                f"首次 新增{first.created} 更新{first.updated}（{elapsed_first:.2f}s）；"
                f"二次 新增{second.created} 跳过{second.skipped}（{elapsed_second:.2f}s）",
                {"first_s": round(elapsed_first, 3), "second_s": round(elapsed_second, 3)},
            )
        else:
            record("图谱导入幂等", "FAIL",
                   f"二次导入应全部跳过，实际 新增{second.created} 更新{second.updated}")

        # ---------- 阶段 3：案例导入幂等 + 嵌入维度 ----------
        cases_file = SMOKE_DIR / "cases_smoke.json"
        t0 = time.perf_counter()
        first = import_case_json(str(cases_file), store)
        elapsed_first = time.perf_counter() - t0
        t0 = time.perf_counter()
        second = import_case_json(str(cases_file), store)
        elapsed_second = time.perf_counter() - t0
        if first.errors:
            record("案例导入", "FAIL", "; ".join(first.errors))
            return 1
        # 检查嵌入向量维度（BGE-M3 = 1024）
        emb_rows = store.execute_read(
            "MATCH (n:Case) WHERE n.embedding IS NOT NULL "
            "RETURN n.case_id AS cid, size(n.embedding) AS dim LIMIT 4",
            {},
        )
        dims = {row["dim"] for row in emb_rows}
        dim_ok = dims == {1024}
        if second.created == 0 and dim_ok:
            note = "嵌入维度 1024 ✓" if not first.warnings else "嵌入服务不可用（已降级）"
            record(
                "案例导入幂等",
                "PASS" if dim_ok else "WARN",
                f"首次 新增{first.created}（{elapsed_first:.2f}s）；"
                f"二次 新增{second.created} 跳过{second.skipped}（{elapsed_second:.2f}s）；{note}",
                {"first_s": round(elapsed_first, 3), "second_s": round(elapsed_second, 3)},
            )
        else:
            record("案例导入幂等", "FAIL",
                   f"二次导入应全部跳过，实际 新增{second.created}；嵌入维度 {dims}")

        # ---------- 阶段 4：案例检索准确率与延迟 ----------
        req_a = WeldingRequirement.model_validate(
            json.loads((SMOKE_DIR / "requirement_smoke_A.json").read_text(encoding="utf-8"))
        )
        t0 = time.perf_counter()
        matches_a = find_similar_cases(req_a, store, top_k=3)
        elapsed = time.perf_counter() - t0
        top1 = matches_a[0].case.case_id if matches_a else None
        # 合成数据已知真值：需求 A 与 case_smoke_002 完全一致
        if top1 == "case_smoke_002":
            scores = ", ".join(
                f"{m.case.case_id}={m.total_score:.3f}"
                f"(结构{m.structured_score:.3f}/语义{m.semantic_score:.3f})"
                for m in matches_a[:3]
            )
            record("案例检索准确率", "PASS", f"Top-1 = case_smoke_002（真值一致）；排序：{scores}",
                   {"elapsed_s": round(elapsed, 3), "top_k": 3})
        else:
            record("案例检索准确率", "FAIL", f"期望 Top-1=case_smoke_002，实际 {top1}")

        # ---------- 阶段 5：差异计算正确性 ----------
        req_b = WeldingRequirement.model_validate(
            json.loads((SMOKE_DIR / "requirement_smoke_B.json").read_text(encoding="utf-8"))
        )
        base_case = matches_a[0].case
        diffs = compare_case(req_b, base_case)
        codes = {d.code for d in diffs}
        # 期望：thickness_increase + 其余字段 same（与比较器的字段别名映射一致）
        from welding_kg.case_comparator import _FIELD_CODE_ALIAS  # noqa: E402

        expected = {"thickness_increase"}
        expected |= {f"{_FIELD_CODE_ALIAS.get(f, f)}_same" for f in
                     ("material", "joint_type", "position", "wire_diameter_mm", "shielding_gas")}
        if codes == expected:
            record("差异计算", "PASS", f"差异代码 {sorted(codes)}（板厚 8→10 判为 increase）")
        else:
            record("差异计算", "FAIL", f"期望 {sorted(expected)}，实际 {sorted(codes)}")

        # ---------- 阶段 6：多跳路径查询正确性与延迟 ----------
        # 直接构造 smoke_ 前缀的差异项：路径查询与比较器解耦，
        # 合成图所有唯一键带 smoke_ 前缀，与真实数据（mini_test）零冲突
        from welding_kg.models import DifferenceItem  # noqa: E402

        smoke_diffs = [
            DifferenceItem(field="thickness_mm", change="increase",
                           code="smoke_thickness_increase", before="8.0", after="10.0"),
            DifferenceItem(field="position", change="changed",
                           code="smoke_position_changed", before="平焊", after="立焊"),
        ]
        t0 = time.perf_counter()
        paths = query_reasoning_paths(smoke_diffs, store, target_quality="smoke_penetration")
        elapsed = time.perf_counter() - t0
        # 结构性校验：首段 SUGGESTS_ADJUSTMENT、总关系 ≤6、终点为 smoke_penetration
        structural_ok = all(
            p.relations[0].type == "SUGGESTS_ADJUSTMENT"
            and len(p.relations) <= 6
            and p.nodes[-1].key == "smoke_penetration"
            and p.nodes[0].label == "Condition"
            and p.nodes[-1].label == "Quality"
            for p in paths
        )
        # 设备限制：路径含 smoke_welding_current 参数时应有 smoke_welder_A 限制
        limits_attached = any(
            "smoke_welder_A" in str(p.equipment_limits) for p in paths
        )
        # 去重：无重复节点序列
        seqs = [tuple(n.key for n in p.nodes) for p in paths]
        dedup_ok = len(seqs) == len(set(seqs))
        if paths and structural_ok and dedup_ok and limits_attached:
            record(
                "路径查询正确性",
                "PASS",
                f"{len(paths)} 条路径（首段 SUGGESTS_ADJUSTMENT、≤6 关系、"
                f"终点 smoke_penetration、去重、设备限制已附加）",
                {"elapsed_s": round(elapsed, 3), "path_count": len(paths)},
            )
        else:
            record(
                "路径查询正确性",
                "FAIL",
                f"路径数 {len(paths)}，结构校验 {structural_ok}，"
                f"去重 {dedup_ok}，设备限制 {limits_attached}",
            )

        # ---------- 阶段 7：LLM 路径选择（合法 id + 非法 id 拒绝） ----------
        t0 = time.perf_counter()
        selection = select_reasoning_paths(req_b, base_case, smoke_diffs, paths)
        elapsed = time.perf_counter() - t0
        valid_ids = {p.path_id for p in paths}
        legit = all(pid in valid_ids for pid in selection.selected_path_ids)
        if selection.selected_path_ids and legit:
            record(
                "LLM 路径选择",
                "PASS",
                f"选中 {selection.selected_path_ids}，理由：{selection.selection_reason[:50]}",
                {"elapsed_s": round(elapsed, 3)},
            )
        else:
            record("LLM 路径选择", "FAIL",
                   f"选中 {selection.selected_path_ids}，警告 {selection.warnings}")

        # 非法 id 拒绝：直接验证纯函数 filter_valid_ids
        from welding_kg.models import PathSelection  # noqa: E402
        from welding_kg.path_planner import filter_valid_ids  # noqa: E402

        fake = filter_valid_ids(
            PathSelection(selected_path_ids=["p999", "not_exist"], selection_reason="测试"),
            paths,
        )
        if fake.selected_path_ids == [] and any(
            "p999" in w for w in fake.warnings
        ):
            record("非法路径拒绝", "PASS", "注入非法 path_id 被全部拒绝并附 warning")
        else:
            record("非法路径拒绝", "FAIL",
                   f"过滤后剩余 {fake.selected_path_ids}，警告 {fake.warnings}")

        # ---------- 阶段 8：vLLM 服务冒烟（TTFT / 吞吐 / 嵌入延迟） ----------
        try:
            llm_client = OpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key or "EMPTY",
                http_client=httpx2.Client(trust_env=False, timeout=300.0),
            )
            t0 = time.perf_counter()
            resp = llm_client.chat.completions.create(
                model=settings.llm_model,
                messages=[{"role": "user", "content": "只回答两个字：正常"}],
                max_tokens=32,
                temperature=0,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            ttft = time.perf_counter() - t0
            usage = resp.usage
            out_tokens = usage.completion_tokens if usage else 0
            tps = out_tokens / max(ttft, 1e-6)
            record("vLLM LLM 服务", "PASS",
                   f"TTFT {ttft:.2f}s，输出 {out_tokens} token，粗吞吐 {tps:.1f} tok/s",
                   {"ttft_s": round(ttft, 3), "tokens": out_tokens,
                    "tokens_per_s": round(tps, 1)})

            emb_client = OpenAI(
                base_url=settings.embedding_base_url,
                api_key=settings.embedding_api_key or "EMPTY",
                http_client=httpx2.Client(trust_env=False, timeout=120.0),
            )
            t0 = time.perf_counter()
            emb_resp = emb_client.embeddings.create(
                model=settings.embedding_model, input=["焊接电流对熔深的影响"])
            emb_latency = time.perf_counter() - t0
            dim = len(emb_resp.data[0].embedding)
            record("vLLM 嵌入服务", "PASS",
                   f"嵌入延迟 {emb_latency:.2f}s，维度 {dim}",
                   {"elapsed_s": round(emb_latency, 3), "dim": dim})
        except Exception as exc:  # noqa: BLE001
            record("vLLM 服务", "FAIL", str(exc))

        # ---------- 阶段 9：收尾清理（冒烟数据不留在库中） ----------
        cleaned = store.execute_write(
            "MATCH (n) WHERE (n.case_id STARTS WITH 'case_smoke_') "
            "OR (n.source_refs IS NOT NULL AND "
            "    any(x IN n.source_refs WHERE x STARTS WITH 'SYNTHETIC')) "
            "DETACH DELETE n RETURN count(n) AS cnt",
            {},
        )
        if cleaned:
            record("冒烟数据收尾清理", "PASS", f"已清理 {cleaned[0]['cnt']} 个合成节点，库中不留测试数据")

        # ---------- 汇总报告 ----------
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = REPORT_DIR / f"smoke_report_{stamp}.json"
        report = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "results": RESULTS,
            "summary": {
                "total": len(RESULTS),
                "passed": sum(1 for r in RESULTS if r["status"] == "PASS"),
                "failed": sum(1 for r in RESULTS if r["status"] == "FAIL"),
                "warned": sum(1 for r in RESULTS if r["status"] == "WARN"),
            },
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n=== 汇总：{report['summary']['passed']} 通过 / "
              f"{report['summary']['failed']} 失败 / {report['summary']['warned']} 告警 ===")
        print(f"报告已写入：{report_path}")
        return 0 if report["summary"]["failed"] == 0 else 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
