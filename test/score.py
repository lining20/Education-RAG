"""离线评分：精确证据检查与要点规则；语义正确性、忠实性、引用支持性由人工复核。"""
import argparse
import csv
import json
import math
from statistics import mean

from test.http_client import successful
from test.settings import RESULTS, dataset_hash, inside, load_cases, normalize

REVIEW_FIELDS = ["correctness", "faithfulness", "citation_accuracy", "refusal_correct"]
REFUSAL_STATUSES = {"no_results", "insufficient_evidence", "clarification_required"}


def average(values):
    return round(mean(values), 6) if values else None


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    offset = (len(ordered) - 1) * fraction
    low, high = math.floor(offset), math.ceil(offset)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (offset - low), 3)


def evidence_matches(evidence, reference):
    # 只评模型实际收到的检索片段，不借完整 record 中未传给模型的正文补分。
    text = reference.get("content", "")[:5000]
    if evidence.get("format") == "table":
        text = text.replace("|", "")
    return (evidence.get("origin") != "image" and reference.get("kind") == "knowledge"
            and reference.get("row", {}).get("source_file_name") == evidence["file"]
            and normalize(evidence["quote"]) in normalize(text))


def chapter_matches(evidence, reference):
    if reference.get("kind") != "knowledge" or reference.get("row", {}).get("source_file_name") != evidence["file"]:
        return False
    payload = reference.get("record", {}).get("payload", {})
    paths = list(payload.get("heading_paths") or [])
    if payload.get("heading_path"):
        paths.append(payload["heading_path"])
    paths.extend(item.get("heading_path", []) for item in payload.get("source_segments", []))
    wanted = normalize(evidence["heading"])
    if any(wanted == normalize(part) for path in paths for part in (path if isinstance(path, list) else [path])):
        return True
    # PDF/Word 的标题未结构化时使用实际返回文本中的标题或原文证据。
    return wanted in normalize(reference.get("content", "")[:5000]) or evidence_matches(evidence, reference)


def case_metrics(case, response, top_k, mode):
    ok = successful(response)
    result = response.get("result") or {}
    refs = result.get("references", [])[:top_k] if ok else []
    evidence = case["evidence"]
    matched = [any(evidence_matches(item, ref) for ref in refs) for item in evidence]
    ranks = [index for index, ref in enumerate(refs, 1) if any(evidence_matches(e, ref) for e in evidence)]
    answer = normalize(result.get("answer", "")) if ok else ""
    checks = [any(normalize(value) in answer for value in check["any_of"]) for check in case["answer_checks"]]
    chapters = list({(e["file"], e["heading"]): e for e in evidence}.values())
    chapter_hits = sum(any(chapter_matches(e, ref) for ref in refs[:5]) for e in chapters)
    image_case = any(e.get("origin") == "image" for e in evidence)
    return {
        "request_success": int(ok),
        "status": result.get("status", "missing_or_failed"),
        "chapter_recall_at_5": chapter_hits / len(chapters) if chapters else None,
        "required_chapters": len(chapters), "retrieved_chapters": chapter_hits,
        "hit_at_k": int(any(matched)) if evidence and not image_case else None,
        "evidence_recall_at_k": sum(matched) / len(matched) if matched and not image_case else None,
        "all_evidence_at_k": int(all(matched)) if matched and not image_case else None,
        "mrr_at_k": (1 / min(ranks) if ranks else 0) if evidence and not image_case else None,
        "image_reference_hit_proxy": int(any(ref.get("images") and any(ref.get("row", {}).get("source_file_name") == e["file"] for e in evidence) for ref in refs)) if image_case else None,
        "answer_fact_coverage_proxy": sum(checks) / len(checks) if checks and mode == "answer" else None,
        "all_answer_facts_proxy": int(all(checks)) if checks and mode == "answer" else None,
        "refusal_status_proxy": int(ok and result.get("status") in REFUSAL_STATUSES) if not evidence and mode == "answer" else None,
        "false_refusal_status_proxy": int(ok and result.get("status") in REFUSAL_STATUSES) if evidence and mode == "answer" else None,
        "missed_evidence": [item for item, found in zip(evidence, matched) if not found],
        "missed_answer_checks": [item["label"] for item, found in zip(case["answer_checks"], checks) if not found] if mode == "answer" else [],
    }


def trial_key(item):
    return item["id"], item["mode"], item["repeat"]


def score_run(manifest, rows, cases):
    if manifest.get("type") != "run" or manifest.get("schema_version") != 1:
        raise ValueError("输入不是受支持的评测记录")
    expected = manifest["expected_trials"]
    expected_keys = {trial_key(item) for item in expected}
    if len(expected_keys) != len(expected) or not expected:
        raise ValueError("评测清单为空或有重复试验")
    actual = {}
    for row in rows:
        key = trial_key(row)
        if row.get("type") != "case" or key in actual or key not in expected_keys:
            raise ValueError("结果重复或不属于本次评测清单")
        actual[key] = row
    by_id = {case["id"]: case for case in cases}
    details = []
    for trial in expected:
        if trial["mode"] not in {"search", "answer"} or trial["id"] not in by_id:
            raise ValueError("评测清单包含未知问题或模式")
        case = by_id[trial["id"]]
        row = actual.get(trial_key(trial))
        response = row["response"] if row else {"error": {"code": "MISSING_RESULT"}}
        metrics = case_metrics(case, response, manifest["options"]["top_k"], trial["mode"])
        details.append({**trial, "category": case["category"], "answerable": case["answerable"],
                        "record_present": row is not None, **metrics,
                        "client_queue_ms": row.get("client_queue_ms") if row else None,
                        "history_elapsed_ms": row.get("history_elapsed_ms") if row else None,
                        "case_elapsed_ms": row.get("case_elapsed_ms") if row else None,
                        "elapsed_ms": response.get("elapsed_ms"), "first_delta_ms": response.get("first_delta_ms")})
    modes = {}
    numeric = ["request_success", "chapter_recall_at_5", "hit_at_k", "evidence_recall_at_k", "all_evidence_at_k", "mrr_at_k", "image_reference_hit_proxy",
               "answer_fact_coverage_proxy", "all_answer_facts_proxy", "refusal_status_proxy", "false_refusal_status_proxy"]
    for mode in dict.fromkeys(item["mode"] for item in expected):
        items = [item for item in details if item["mode"] == mode]
        summary = {"trials": len(items), "answerable_trials": sum(item["answerable"] for item in items),
                   "unanswerable_trials": sum(not item["answerable"] for item in items),
                   "missing_results": sum(not item["record_present"] for item in items)}
        summary.update({field: {"value": average([item[field] for item in items if item[field] is not None]),
                                "denominator": sum(item[field] is not None for item in items)} for field in numeric})
        for field in ["elapsed_ms", "first_delta_ms"]:
            values = [item[field] for item in items if item["request_success"] and item[field] is not None]
            summary[field + "_successful"] = {"p50": percentile(values, .5), "p95": percentile(values, .95), "count": len(values)}
        summary["by_category"] = {category: {
            "count": len(group), "request_success": average([v["request_success"] for v in group]),
            "evidence_recall_at_k": average([v["evidence_recall_at_k"] for v in group if v["evidence_recall_at_k"] is not None])}
            for category in sorted({v["category"] for v in items})
            for group in [[v for v in items if v["category"] == category]]}
        modes[mode] = summary
    return {"run_id": manifest["run_id"], "dataset_sha256": manifest["dataset_sha256"],
            "concurrency": manifest["options"].get("concurrency", 1),
            "execution": manifest["options"].get("execution", "legacy_serial"),
            "timing": manifest.get("timing", "历史计时口径见原始记录"),
            "top_k": manifest["options"]["top_k"], "modes": modes, "details": details,
            "semantic_scores": "见人工复核，规则分数不能替代语义正确性。"}


def review_rows(manifest, rows, cases):
    by_id = {case["id"]: case for case in cases}
    actual = {trial_key(row): row for row in rows}
    for trial in manifest["expected_trials"]:
        if trial["mode"] != "answer":
            continue
        case = by_id[trial["id"]]
        row = actual.get(trial_key(trial), {})
        response = row.get("response", {})
        result = response.get("result") or {}
        yield {"id": trial["id"], "mode": trial["mode"], "repeat": trial["repeat"],
               "question": case["question"], "history": json.dumps(case["history"], ensure_ascii=False),
               "answerable": case["answerable"], "expected_answer": case["expected_answer"],
               "request_success": int(successful(response)),
               "actual_answer": result.get("answer", ""), "status": result.get("status", "missing_or_failed"),
               "gold_evidence": json.dumps(case["evidence"], ensure_ascii=False),
               "retrieved_evidence": json.dumps([{"id": ref.get("id"), "file": ref.get("row", {}).get("source_file_name"),
                                                  "content": ref.get("content", "")[:5000]}
                                                 for ref in result.get("references", [])], ensure_ascii=False),
               **{field: "" for field in REVIEW_FIELDS}, "substantive_answer": "", "reviewer": "", "notes": ""}


def core_summary(report, review_path, expected_rows):
    """截图对应的六项核心指标。语义指标采用案例全通过率，未复核保持空值。"""
    selected_mode = "answer" if "answer" in report["modes"] else "search"
    details = [d for d in report["details"] if d["mode"] == selected_mode]
    chapters = [d["chapter_recall_at_5"] for d in details if d["chapter_recall_at_5"] is not None]
    reviewed = {}
    if review_path.exists():
        with review_path.open(encoding="utf-8-sig", newline="") as source:
            reviewed = {(r["id"], r["mode"], str(r["repeat"])): r for r in csv.DictReader(source)}
    positive, negative, substantive = [], [], []
    scope_pending = 0
    for template in expected_rows:
        key = template["id"], template["mode"], str(template["repeat"])
        row = reviewed.get(key, {})
        failed = not template["request_success"]
        def value(field):
            raw = row.get(field, "").strip()
            return float(raw) if raw else None
        if not template["answerable"]:
            negative.append(0 if failed else value("refusal_correct"))
            continue
        positive.append(0 if failed else value("correctness"))
        raw = row.get("substantive_answer", "").strip()
        if raw not in {"", "0", "1"}:
            raise ValueError("substantive_answer 只允许 0、1 或留空")
        if failed or raw == "0":
            continue
        if raw == "1":
            substantive.append((value("faithfulness"), value("citation_accuracy")))
        else:
            scope_pending += 1
    def ratio(values, pending=0):
        known = [v for v in values if v is not None]
        return {"value": sum(v == 1 for v in known) / len(values) if values and len(known) == len(values) and not pending else None,
                "passed": sum(v == 1 for v in known), "reviewed": len(known), "denominator": len(values), "scope_pending": pending}
    latency = report["modes"].get("answer", {}).get("elapsed_ms_successful", {})
    return {
        "章节 Recall@5": {"value": average(chapters), "denominator": len(chapters), "mode": selected_mode,
                        "effective_k": min(5, report["top_k"]), "matched_chapters": sum(d["retrieved_chapters"] for d in details),
                        "required_chapters": sum(d["required_chapters"] for d in details)},
        "回答正确完整率": ratio(positive),
        "回答依据一致率": ratio([v[0] for v in substantive], scope_pending),
        "严格引用支持率": ratio([v[1] for v in substantive], scope_pending),
        "无答案题正确拒答": ratio(negative),
        "问答耗时 P50 / P95": {"p50_seconds": latency.get("p50") / 1000 if latency.get("p50") is not None else None,
                             "p95_seconds": latency.get("p95") / 1000 if latency.get("p95") is not None else None,
                             "count": latency.get("count", 0)},
    }


def manual_summary(path, expected_rows):
    expected = {(row["id"], row["mode"], str(row["repeat"])): row for row in expected_rows}
    values = {field: [] for field in REVIEW_FIELDS}
    seen = set()
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not set(REVIEW_FIELDS + ["id", "mode", "repeat"]).issubset(reader.fieldnames or []):
            raise ValueError("人工复核 CSV 缺少列")
        for row in reader:
            key = row["id"], row["mode"], row["repeat"]
            if key not in expected or key in seen:
                raise ValueError("人工复核包含重复或未知案例")
            seen.add(key)
            for field in REVIEW_FIELDS:
                raw = row[field].strip()
                if not raw:
                    continue
                value = float(raw)
                if value not in {0, .5, 1}:
                    raise ValueError("人工分数只允许 0、0.5、1 或留空")
                if field == "refusal_correct" and expected[key]["answerable"]:
                    raise ValueError("refusal_correct 只适用于无答案案例")
                if field in {"faithfulness", "citation_accuracy"} and not expected[key]["answerable"]:
                    raise ValueError("无答案案例请留空 faithfulness 和 citation_accuracy")
                values[field].append(value)
    return {field: {"value": average(scores), "reviewed": len(scores),
                    "eligible": sum(not row["answerable"] if field == "refusal_correct" else
                                    row["answerable"] if field in {"faithfulness", "citation_accuracy"} else True
                                    for row in expected_rows)} for field, scores in values.items()}


def write_report(directory, report, review):
    with (directory / "metrics.json").open("w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    lines = ["# RAG 评测报告", "", f"Run: {report['run_id']}", "", f"Top-K: {report['top_k']}", "",
             f"并发案例数：{report.get('concurrency', 1)}；执行方式：{report.get('execution', 'legacy_serial')}。", "",
             "本题响应耗时从本题发送开始，到本题SSE done结束；前置对话完整结束后才发送追问并重新计时。客户端等待并发名额与前置问答耗时单独记录。服务端排队属于本次实际响应耗时。", "",
             "## 核心指标", "", "| 指标 | 结果 |", "| --- | ---: |"]
    for name, item in report.get("core", {}).items():
        if name == "问答耗时 P50 / P95":
            display = f"{item['p50_seconds']:.2f} / {item['p95_seconds']:.2f} 秒" if item["p50_seconds"] is not None else "暂无问答耗时"
        elif name == "章节 Recall@5":
            display = f"{item['value']:.1%}" if item["value"] is not None else "暂无有答案案例"
        elif item["value"] is None:
            if not item["denominator"] and not item["scope_pending"]:
                display = "暂无适用案例"
            else:
                display = f"待复核（已评 {item['reviewed']}/{item['denominator']}"
                display += f"，另有 {item['scope_pending']} 条待判定是否实质回答）" if item["scope_pending"] else "）"
        else:
            display = f"{item['passed']}/{item['denominator']}" if name == "无答案题正确拒答" else f"{item['value']:.1%}（{item['passed']}/{item['denominator']}）"
        lines.append(f"| {name} | {display} |")
    lines += ["", "章节 Recall@5 对每题必需章节去重后计算召回比例，再对有答案案例取平均；优先展示问答轮，只有搜索轮时展示搜索轮。", "",
              "三项回答语义指标及正确拒答使用人工复核，只有全部适用案例完成复核后才显示最终比例。依据一致率和严格引用支持率的分母是确认给出实质回答的有答案案例。0.5分不计为严格通过。", "",
              "本次实际返回数量上限小于5时，章节 Recall@5仅反映这些返回结果，不能与完整Top-5条件直接比较。", "",
             "失败和缺失结果计入分母；没有真实评测记录时不会生成性能成绩。", "",
             "证据匹配基于文件名和原文摘录；要点命中与拒答状态只是规则代理指标。", ""]
    for mode, summary in report["modes"].items():
        lines += [f"## {mode}", "", f"计划 {summary['trials']} 次；缺失 {summary['missing_results']} 次。", "",
                  "| 指标 | 值 | 分母 |", "| --- | --- | --- |"]
        for field, metric in summary.items():
            if isinstance(metric, dict) and "value" in metric:
                lines.append(f"| {field} | {metric['value']} | {metric['denominator']} |")
        lines += ["", "成功请求延迟（毫秒）：", "", "```json", json.dumps({key: summary[key] for key in
                  ["elapsed_ms_successful", "first_delta_ms_successful"]}, ensure_ascii=False, indent=2), "```", ""]
    lines += ["## 人工复核", "", f"填写 `{review.name}` 后重新执行相同评分命令。", "",
              "```json", json.dumps(report["manual"], ensure_ascii=False, indent=2), "```", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=str, help="test/results/某次运行/responses.jsonl")
    parser.add_argument("--dataset", choices=["current", "v1"], default="current", help="v1用于原始20题历史结果重评分")
    args = parser.parse_args()
    path = inside(args.input, RESULTS)
    cases = load_cases(args.dataset)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records or records[0].get("dataset_sha256") != dataset_hash(args.dataset):
        raise ValueError("结果为空或数据集已变化，请使用相同版本重新评测")
    manifest, rows = records[0], records[1:]
    report = score_run(manifest, rows, cases)
    review = path.parent / "review.csv"
    template = list(review_rows(manifest, rows, cases))
    if template and not review.exists():
        with review.open("x", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(template[0]))
            writer.writeheader()
            writer.writerows(template)
    report["manual"] = manual_summary(review, template) if template else {}
    report["core"] = core_summary(report, review, template)
    write_report(path.parent, report, review)
    print("报告：", path.parent / "report.md")
    print("人工复核：", review if template else "本轮只有搜索模式，无回答复核")


if __name__ == "__main__":
    main()
