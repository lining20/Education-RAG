"""只验证评测工具自身，不导入业务应用，不访问网络或数据库。"""
from contextlib import redirect_stdout
import asyncio
import csv
from email.message import Message
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from test import import_data, run_eval, score, settings
from test.http_client import Client, iter_sse, successful


def reference(evidence, content=None):
    return {"id": "1", "kind": "knowledge", "row": {"source_file_name": evidence["file"]},
            "content": evidence["quote"] if content is None else content}


def capture(refs=(), answer="", status="completed"):
    return {"done": True, "error": None, "elapsed_ms": 10, "first_delta_ms": 3,
            "result": {"status": status, "answer": answer, "references": list(refs)}}


def manifest(trials):
    return {"type": "run", "schema_version": 1, "run_id": "offline-fixture",
            "dataset_sha256": settings.dataset_hash(), "options": {"top_k": 5}, "expected_trials": trials}


class Stream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = "text/event-stream; charset=utf-8"


class OfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = settings.load_cases()

    def test_dataset_count_and_evidence_are_valid(self):
        self.assertEqual(len(self.cases), 40)
        self.assertEqual(sum(c["answerable"] for c in self.cases), 35)
        self.assertEqual(len(list(settings.DOCUMENTS.glob("*.md"))), 4)
        self.assertEqual(len(settings.list_documents()), 6)

    def test_dataset_hash_is_stable(self):
        self.assertEqual(settings.dataset_hash(), settings.dataset_hash())
        self.assertEqual(len(settings.dataset_hash()), 64)

    def test_output_path_cannot_escape(self):
        with self.assertRaises(ValueError):
            settings.inside(settings.ROOT.parent / "outside.json", settings.RESULTS)

    def test_remote_and_credential_urls_are_rejected(self):
        for url in ["https://example.com", "http://user:pass@localhost:8201", "http://localhost:8201/query"]:
            with self.assertRaises(ValueError):
                Client(url)

    def test_identity_mismatch_blocks_connection(self):
        with patch.object(Client, "json", return_value={"suite": "wrong"}):
            with self.assertRaises(RuntimeError):
                Client(settings.BASE_URL).verify()

    def test_sse_crlf_comments_and_multiline(self):
        data = b': ping\r\nevent: result\r\ndata: {"status":\r\ndata: "completed"}\r\n\r\n'
        self.assertEqual(list(iter_sse(io.BytesIO(data))), [("result", {"status": "completed"})])

    def test_sse_query_collects_final_result(self):
        data = ('event: delta\ndata: {"text":"开始"}\n\n'
                'event: result\ndata: {"status":"completed","answer":"23天","references":[]}\n\n'
                'event: done\ndata: {}\n\n').encode("utf-8")
        client = Client(settings.BASE_URL)
        with patch.object(client, "open", return_value=Stream(data)):
            result = client.query({"question": "测试"})
        self.assertTrue(successful(result))
        self.assertIsNotNone(result["first_delta_ms"])

    def test_truncated_sse_is_failure(self):
        client = Client(settings.BASE_URL)
        with patch.object(client, "open", return_value=Stream(b'event: result\ndata: {"status":"completed"}\n\n')):
            self.assertFalse(successful(client.query({})))

    def test_sse_error_is_not_success_even_with_http_200(self):
        client = Client(settings.BASE_URL)
        with patch.object(client, "open", return_value=Stream(b'event: error\ndata: {"code":"FAILED"}\n\nevent: done\ndata: {}\n\n')):
            self.assertFalse(successful(client.query({})))

    def test_hit_recall_and_mrr_use_evidence_units(self):
        case = self.cases[9]  # 两份文件中的两条证据
        refs = [reference({"file": "wrong.md", "quote": "干扰"}), reference(case["evidence"][0])]
        result = score.case_metrics(case, capture(refs), 5, "search")
        self.assertEqual(result["hit_at_k"], 1)
        self.assertEqual(result["evidence_recall_at_k"], .5)
        self.assertEqual(result["mrr_at_k"], .5)
        self.assertEqual(result["all_evidence_at_k"], 0)

    def test_same_document_with_wrong_passage_is_not_evidence(self):
        case = self.cases[0]
        ref = reference(case["evidence"][0], "这个课程挺好")
        self.assertEqual(score.case_metrics(case, capture([ref]), 5, "search")["hit_at_k"], 0)

    def test_hidden_full_record_cannot_inflate_evidence_score(self):
        case = self.cases[0]
        ref = reference(case["evidence"][0], "片段缺少答案")
        ref["record"] = {"content": case["evidence"][0]["quote"]}
        self.assertFalse(score.evidence_matches(case["evidence"][0], ref))
        ref["content"] = "x" * 5000 + case["evidence"][0]["quote"]
        self.assertFalse(score.evidence_matches(case["evidence"][0], ref))

    def test_missing_result_is_in_denominator(self):
        trials = [{"id": "q01", "mode": "search", "repeat": n} for n in [1, 2]]
        row = {"type": "case", **trials[0], "response": capture([reference(self.cases[0]["evidence"][0])])}
        report = score.score_run(manifest(trials), [row], self.cases)
        result = report["modes"]["search"]
        self.assertEqual(result["hit_at_k"], {"value": .5, "denominator": 2})
        self.assertEqual(result["missing_results"], 1)

    def test_failure_is_not_correct_refusal(self):
        result = score.case_metrics(self.cases[16], {"error": {"code": "NETWORK"}}, 5, "answer")
        self.assertEqual(result["refusal_status_proxy"], 0)
        self.assertEqual(result["request_success"], 0)

    def test_duplicate_trial_results_are_rejected(self):
        trial = {"id": "q01", "mode": "answer", "repeat": 1}
        row = {"type": "case", **trial, "response": capture()}
        with self.assertRaises(ValueError):
            score.score_run(manifest([trial]), [row, row], self.cases)

    def test_history_failure_prevents_target_query(self):
        client = Client(settings.BASE_URL)
        with patch.object(client, "query", new_callable=AsyncMock, return_value={"done": False, "error": {"code": "FAILED"}}) as query:
            row = asyncio.run(run_eval.evaluate_case(client, self.cases[14], mode="answer", top_k=5,
                                                    use_hyde=False, run_id="offline", repeat=1))
        self.assertEqual(query.call_count, 1)
        self.assertEqual(row["response"]["error"]["code"], "HISTORY_SETUP_FAILED")

    def test_history_receives_questions_without_gold_answers(self):
        client = Client(settings.BASE_URL)
        with patch.object(client, "query", new_callable=AsyncMock, return_value=capture(answer="由真实系统生成")) as query:
            asyncio.run(run_eval.evaluate_case(client, self.cases[15], mode="search", top_k=5,
                                              use_hyde=False, run_id="offline", repeat=1))
        first, last = [call.args[0] for call in query.call_args_list]
        self.assertEqual(first["session_id"], last["session_id"])
        self.assertEqual(first["question"], self.cases[15]["history"][0])
        self.assertNotIn("expected_answer", first)
        self.assertNotEqual(first["request_id"], last["request_id"])

    def test_default_commands_do_not_construct_client(self):
        for module in [import_data, run_eval]:
            name = "Client" if module is import_data else "AsyncClient"
            with patch.object(sys, "argv", [module.__name__]), patch.object(module, name, side_effect=AssertionError("不应联网")), redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(), 0)

    def test_multipart_contains_only_documents(self):
        body, kind = import_data.multipart(settings.list_documents())
        self.assertIn("multipart/form-data", kind)
        self.assertEqual(body.count(b'name="file"'), 6)
        self.assertNotIn(b"questions.jsonl", body)
        self.assertIn("青禾".encode(), body)
        self.assertIn(b"application/pdf", body)
        self.assertIn(b"application/vnd.openxmlformats-officedocument.wordprocessingml.document", body)

    def test_archived_v1_retains_original_hash(self):
        self.assertEqual(len(settings.load_cases("v1")), 20)
        self.assertEqual(settings.dataset_hash("v1"), "854ef15c84a51f196d02c67dbffd11f6bad59d922d233404a5f525143cb85d98")

    def test_long_md_and_code_are_present(self):
        text = settings.source_text("rag_test_01_青禾课程说明.md")
        self.assertGreater(len(text.split("## 学习成果复核", 1)[1]), 1000)
        code = settings.source_text("rag_test_04_澄石项目手册.md").split("```python", 1)[1].split("```", 1)[0]
        self.assertGreater(len(code), 1000)
        self.assertIn('CODE_END_MARKER = "CS-CODE-END-73"', code)

    def test_binary_source_evidence_and_images_are_separate(self):
        self.assertIn("设备检查记录", settings.source_text("rag_test_05_星桥实验室操作规程.docx"))
        self.assertIn("BC-EVAL-42", settings.source_text("rag_test_06_北辰评测方案.pdf"))
        self.assertNotIn("17分钟", settings.source_text("rag_test_05_星桥实验室操作规程.docx"))
        self.assertNotIn("86", settings.source_text("rag_test_06_北辰评测方案.pdf"))
        self.assertEqual(sum("vision" in c.get("requires", []) for c in self.cases), 2)

    def test_chapter_recall_deduplicates_evidence_in_one_chapter(self):
        case = self.cases[13]  # 同一章节中的两条事实
        ref = reference(case["evidence"][0], "片段只提供本节部分事实")
        ref["record"] = {"payload": {"heading_path": ["澄石项目手册", "批处理配置"]}}
        result = score.case_metrics(case, capture([ref]), 5, "answer")
        self.assertEqual(result["chapter_recall_at_5"], 1)
        self.assertEqual(result["required_chapters"], 1)
        self.assertEqual(result["evidence_recall_at_k"], 0)

    def test_image_evidence_never_gets_fake_exact_text_score(self):
        case = self.cases[32]
        result = score.case_metrics(case, capture([reference(case["evidence"][0])]), 5, "answer")
        self.assertIsNone(result["evidence_recall_at_k"])

    def test_table_evidence_tolerates_markdown_delimiters(self):
        evidence = self.cases[27]["evidence"][0]
        self.assertTrue(score.evidence_matches(evidence, reference(evidence, "| 设备检查记录 | 45天 | 核验用途 |")))
        self.assertFalse(score.evidence_matches(evidence, reference(evidence, "| 设备检查记录 | 90天 | 核验用途 |")))

    def test_six_core_metrics_require_full_semantic_review(self):
        trial = {"id": "q01", "mode": "answer", "repeat": 1}
        header = manifest([trial])
        rows = [{"type": "case", **trial, "response": capture([reference(self.cases[0]["evidence"][0])], "23天[1]")}]
        report = score.score_run(header, rows, self.cases)
        template = list(score.review_rows(header, rows, self.cases))
        settings.RESULTS.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=settings.RESULTS) as temporary:
            path = Path(temporary) / "review.csv"
            result = score.core_summary(report, path, template)
            self.assertEqual(len(result), 6)
            self.assertIsNone(result["回答正确完整率"]["value"])
            template[0].update(correctness=1, faithfulness=1, citation_accuracy=.5, substantive_answer=1)
            with path.open("w", encoding="utf-8-sig", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=list(template[0]))
                writer.writeheader(); writer.writerows(template)
            result = score.core_summary(report, path, template)
            self.assertEqual(result["回答正确完整率"]["value"], 1)
            self.assertEqual(result["回答依据一致率"]["value"], 1)
            self.assertEqual(result["严格引用支持率"]["value"], 0)

    def test_default_run_excludes_only_image_questions(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["run_eval"]), patch.object(run_eval, "AsyncClient", side_effect=AssertionError("不能联网")), redirect_stdout(output):
            run_eval.main()
        self.assertIn("38", output.getvalue())

    def test_md_only_run_excludes_cross_format_and_word_negatives(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["run_eval", "--formats", "md"]), patch.object(run_eval, "AsyncClient", side_effect=AssertionError("不能联网")), redirect_stdout(output):
            run_eval.main()
        self.assertIn("25 个案例", output.getvalue())

    def test_report_and_manual_review_are_offline_and_unscored(self):
        trials = [{"id": "q01", "mode": "answer", "repeat": 1}]
        rows = [{"type": "case", **trials[0], "response": capture([reference(self.cases[0]["evidence"][0])], "23天[1]")}]
        header = manifest(trials)
        settings.RESULTS.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=settings.RESULTS) as temporary:
            path = Path(temporary) / "responses.jsonl"
            path.write_text("\n".join(json.dumps(v, ensure_ascii=False) for v in [header, *rows]), encoding="utf-8")
            with patch.object(sys, "argv", ["score", "--input", str(path)]), redirect_stdout(io.StringIO()):
                score.main()
                score.main()  # 再评分保留人工复核文件
            result = json.loads((path.parent / "metrics.json").read_text(encoding="utf-8"))
            self.assertIsNone(result["manual"]["correctness"]["value"])
            self.assertEqual(result["manual"]["correctness"]["reviewed"], 0)
            self.assertTrue((path.parent / "report.md").exists())

    def test_manual_values_and_applicability_are_validated(self):
        expected = [{"id": "q17", "mode": "answer", "repeat": 1, "answerable": False}]
        settings.RESULTS.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=settings.RESULTS) as temporary:
            path = Path(temporary) / "review.csv"
            with path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=["id", "mode", "repeat", *score.REVIEW_FIELDS])
                writer.writeheader()
                writer.writerow({"id": "q17", "mode": "answer", "repeat": 1, "refusal_correct": 1})
            result = score.manual_summary(path, expected)
            self.assertEqual(result["refusal_correct"]["value"], 1)
            self.assertIsNone(result["faithfulness"]["value"])

    def test_percentile_empty_and_interpolation(self):
        self.assertIsNone(score.percentile([], .95))
        self.assertEqual(score.percentile([10, 20], .95), 19.5)


if __name__ == "__main__":
    unittest.main()
