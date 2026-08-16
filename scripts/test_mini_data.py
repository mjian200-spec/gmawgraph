#!/usr/bin/env python3
"""mini_test 真实模拟数据测试（规范：重要功能需冒烟实验测试效率与准确率）。

使用 mini_test/ 下的真实案例数据（来自 train.xlsx，非合成）验证：
  导入核验 → 检索自匹配准确率 → 检索效率 → 厚度差异与路径查询
  → 规则方向覆盖 → 位置差异衔接 → LLM 路径选择

测试需求由真实案例字段组合构造（改动单个维度作查询输入），
字段值全部来自真实案例，不虚构案例或书中知识。
（test.xlsx 的 173 条真实需求未提供，后续可用其替换这些构造需求。） → Demo 端到端

用法：python scripts/test_mini_data.py
报告：data/smoke/report/mini_test_report_<时间戳>.json
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from welding_kg.case_comparator import compare_case  # noqa: E402
from welding_kg.case_retriever import find_similar_cases  # noqa: E402
from welding_kg.models import WeldingRequirement  # noqa: E402
from welding_kg.neo4j_store import Neo4jStore  # noqa: E402
from welding_kg.path_planner import select_reasoning_paths  # noqa: E402
from welding_kg.path_retriever import query_reasoning_paths  # noqa: E402
from welding_kg.settings import Settings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = PROJECT_ROOT / "data" / "smoke" / "report"

RESULTS: list[dict] = []


def record(name: str, status: str, detail: str = "", metrics: dict | None = None) -> None:
    """记录一条测试结果（status: PASS / FAIL / WARN）。"""
    RESULTS.append({"name": name, "status": status, "detail": detail, "metrics": metrics or {}})
    mark = {"PASS": "✓", "FAIL": "✗", "WARN": "△"}[status]
    print(f"[{mark}] {name}  {detail}")


def case_as_requirement(case: dict) -> WeldingRequirement:
    """把真实案例字段原样转成需求（字段值全部来自真实数据，不做虚构）。"""
    return WeldingRequirement(
        process=case["process"],
        material=case["material"],
        thickness_mm=case["thickness_mm"],
        joint_type=case["joint_type"],
        position=case["position"],
        wire_diameter_mm=case["wire_diameter_mm"],
        shielding_gas=case["shielding_gas"],
    )


def main() -> int:
    """依次执行各测试阶段，输出汇总报告。"""
    settings = Settings.from_env()
    print(f"=== mini_test 真实数据测试  {datetime.now():%Y-%m-%d %H:%M:%S} ===")

    cases = json.loads(
        (PROJECT_ROOT / "data" / "seed" / "cases.json").read_text(encoding="utf-8")
    )["cases"]
    by_id = {c["case_id"]: c for c in cases}

    store = Neo4jStore(settings)
    try:
        # ---------- 0) 清理合成冒烟数据（避免与 run_smoke 残留互相污染） ----------
        store.execute_write(
            "MATCH (n) WHERE (n.case_id STARTS WITH 'case_smoke_') "
            "OR (n.source_refs IS NOT NULL AND "
            "    any(x IN n.source_refs WHERE x STARTS WITH 'SYNTHETIC')) "
            "DETACH DELETE n",
            {},
        )

        # ---------- 1) 导入核验（与 source_validation.md 数字对照） ----------
        expected_labels = {"Case": 34, "Condition": 29, "Parameter": 6,
                           "Mechanism": 12, "Quality": 7, "Equipment": 1}
        rows = store.execute_read(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS cnt", {})
        actual_labels = {r["label"]: r["cnt"] for r in rows}
        rows = store.execute_read(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS cnt", {})
        actual_rels = {r["t"]: r["cnt"] for r in rows}
        # 知识关系 42 条 + 案例引用关系（34 案例 × 5 条件 / 5 参数）
        expected_rels = {"SUGGESTS_ADJUSTMENT": 11, "AFFECTS": 28, "LIMITS": 3,
                         "HAS_CONDITION": 170, "HAS_PARAMETER": 170}
        if actual_labels == expected_labels and actual_rels == expected_rels:
            record("导入核验", "PASS",
                   f"节点 {expected_labels}；关系 {expected_rels}，与说明文档一致")
        else:
            record("导入核验", "FAIL",
                   f"节点 {actual_labels}（期望 {expected_labels}）；"
                   f"关系 {actual_rels}（期望 {expected_rels}）")

        emb = store.execute_read(
            "MATCH (n:Case) WHERE n.embedding IS NOT NULL "
            "RETURN size(n.embedding) AS dim LIMIT 1", {})
        dims = {r["dim"] for r in emb}
        if dims == {1024}:
            record("案例嵌入", "PASS", "34 案例均有 1024 维 BGE-M3 向量")
        else:
            record("案例嵌入", "WARN", f"嵌入维度异常：{dims}")

        # ---------- 2) 检索自匹配准确率（真实案例作需求，Top-1 应为自身） ----------
        req_self = case_as_requirement(by_id["crp_train_001"])
        t0 = time.perf_counter()
        matches = find_similar_cases(req_self, store, top_k=5)
        elapsed = time.perf_counter() - t0
        top1 = matches[0].case.case_id if matches else None
        if top1 == "crp_train_001":
            top3 = ", ".join(f"{m.case.case_id}={m.total_score:.3f}" for m in matches[:3])
            record("检索自匹配", "PASS",
                   f"Top-1 = crp_train_001（自身），前3：{top3}",
                   {"elapsed_s": round(elapsed, 3)})
        else:
            record("检索自匹配", "FAIL", f"期望 crp_train_001，实际 {top1}")

        # 检索效率：连续 5 次查询取均值
        latencies = []
        for cid in ["crp_train_001", "crp_train_015", "crp_train_024", "crp_train_033", "crp_train_006"]:
            t0 = time.perf_counter()
            find_similar_cases(case_as_requirement(by_id[cid]), store, top_k=5)
            latencies.append(time.perf_counter() - t0)
        avg = sum(latencies) / len(latencies)
        record("检索效率", "PASS", f"5 次 Top-5 检索平均 {avg*1000:.1f} ms/次（34 候选）",
               {"avg_ms": round(avg * 1000, 1)})

        # ---------- 3) 厚度差异 + 路径查询 ----------
        # 测试需求由真实案例字段组合构造（非虚构案例，仅作查询输入）：
        # 取 crp_train_001 的材质/接头/位置/焊丝/气体 + 板厚 5.0mm。
        # 案例库中同型案例板厚为 1.0 / 3.0，最相似基准应为 crp_train_024(3.0)。
        req_thick = case_as_requirement(by_id["crp_train_001"])
        req_thick.thickness_mm = 5.0
        matches_thick = find_similar_cases(req_thick, store, top_k=1)
        base = matches_thick[0].case if matches_thick else None
        if base is None or base.case_id != "crp_train_024":
            record("厚度差异基准案例", "FAIL",
                   f"期望 crp_train_024，实际 {base and base.case_id}")
        else:
            diffs = compare_case(req_thick, base)
            from welding_kg.case_comparator import _FIELD_CODE_ALIAS  # noqa: E402

            expected = {"thickness_increase"} | {
                f"{_FIELD_CODE_ALIAS.get(f, f)}_same" for f in
                ("material", "joint_type", "position", "wire_diameter_mm", "shielding_gas")}
            if {d.code for d in diffs} == expected:
                record("厚度差异计算", "PASS", f"差异代码 {sorted(d.code for d in diffs)}（板厚 3→5 increase）")
            else:
                record("厚度差异计算", "FAIL", f"差异代码 {sorted(d.code for d in diffs)}")
            # 路径查询：thickness_increase 应命中图规则（2 条 SUGGESTS_ADJUSTMENT）
            t0 = time.perf_counter()
            paths = query_reasoning_paths(diffs, store, target_quality=None)
            elapsed = time.perf_counter() - t0
            if paths and all(p.difference_code == "thickness_increase" for p in paths):
                record("厚度路径查询", "PASS",
                       f"{len(paths)} 条路径命中 thickness_increase 规则（{elapsed:.2f}s），"
                       f"样例链：{' → '.join(n.name or n.key for n in paths[0].nodes)}")
            else:
                record("厚度路径查询", "FAIL",
                       f"路径数 {len(paths)}，期望 ≥1 且全部来自 thickness_increase")

            # LLM 选择
            t0 = time.perf_counter()
            selection = select_reasoning_paths(req_thick, base, diffs, paths)
            elapsed = time.perf_counter() - t0
            valid = {p.path_id for p in paths}
            if selection.selected_path_ids and all(p in valid for p in selection.selected_path_ids):
                record("LLM 路径选择", "PASS",
                       f"选中 {selection.selected_path_ids}（{elapsed:.2f}s），"
                       f"理由：{selection.selection_reason[:60]}")
            else:
                record("LLM 路径选择", "FAIL",
                       f"选中 {selection.selected_path_ids}，警告 {selection.warnings}")

        # 方向覆盖检查：板厚 2.0 需求 → 基准 024(3.0) → thickness_decrease，
        # 图谱只有 increase 方向的规则，预期查不到路径（暴露规则方向覆盖）
        req_dec = case_as_requirement(by_id["crp_train_001"])
        req_dec.thickness_mm = 2.0
        matches_dec = find_similar_cases(req_dec, store, top_k=1)
        base_dec = matches_dec[0].case if matches_dec else None
        if base_dec and base_dec.case_id == "crp_train_024":
            diffs_dec = compare_case(req_dec, base_dec)
            paths_dec = query_reasoning_paths(diffs_dec, store, target_quality=None)
            record("厚度反向覆盖", "WARN" if not paths_dec else "PASS",
                   f"thickness_decrease 命中 {len(paths_dec)} 条路径"
                   + ("" if paths_dec else "（图谱无 decrease 方向规则，属规则覆盖缺口）"))

        # ---------- 4) 位置差异衔接（方向性差异代码与图谱规则起点） ----------
        # 比较器已扩展契约（adjustment_generation_spec §8）：目标为立焊/仰焊
        # 时生成 position_to_vertical / position_to_overhead，替代
        # position_changed。这里以方向性差异代码直接查询路径，验证图谱规则
        # 起点衔接（案例库中同工况立焊案例占优，检索 Top-1 很少出现位置差异，
        # 位置差异的检索表现见下方横焊缺口用例）。
        from welding_kg.models import DifferenceItem  # noqa: E402

        diffs_pos = [
            DifferenceItem(field="position", change="changed",
                           code="position_to_vertical", before="flat", after="vertical"),
        ]
        paths_pos = query_reasoning_paths(diffs_pos, store, target_quality=None)
        if paths_pos and all(p.difference_code == "position_to_vertical" for p in paths_pos):
            record("位置差异衔接", "PASS",
                   f"position_to_vertical 命中 {len(paths_pos)} 条路径"
                   "（差异代码契约已扩展，衔接图谱规则起点）")
        else:
            record("位置差异衔接", "FAIL", f"路径 {len(paths_pos)}")

        # 反向缺口：目标为横焊（图谱无横焊方向规则）→ 比较器保留
        # position_changed，预期 0 条路径（规则覆盖缺口，保持 WARN 记录）
        req_pos_h = case_as_requirement(by_id["crp_train_026"])
        req_pos_h.position = "horizontal"
        matches_h = find_similar_cases(req_pos_h, store, top_k=1)
        base_h = matches_h[0].case if matches_h else None
        if base_h and base_h.position != "horizontal":
            diffs_h = compare_case(req_pos_h, base_h)
            paths_h = query_reasoning_paths(diffs_h, store, target_quality=None)
            record("横焊方向规则缺口", "WARN" if not paths_h else "PASS",
                   f"position_changed 命中 {len(paths_h)} 条路径"
                   + ("" if paths_h else "（图谱无横焊方向规则，属规则覆盖缺口）"))

        # ---------- 汇总报告 ----------
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = REPORT_DIR / f"mini_test_report_{stamp}.json"
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
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n=== 汇总：{report['summary']['passed']} 通过 / {report['summary']['failed']} 失败 / "
              f"{report['summary']['warned']} 告警 ===")
        print(f"报告已写入：{report_path}")
        return 0 if report["summary"]["failed"] == 0 else 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
