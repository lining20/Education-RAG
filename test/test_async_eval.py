"""使用协程与内存HTTP传输验证并发、SSE时序和计时；不连接服务。"""
import asyncio
import io
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from test.async_client import AsyncClient, iter_sse
from test.http_client import successful
from test import run_eval, score, settings


def capture():
    return {"done": True, "error": None, "elapsed_ms": 10, "first_delta_ms": 3,
            "result": {"status": "completed", "answer": "回答[1]", "references": []}}


class AsyncEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_run_writes_incremental_results_and_summary(self):
        cases = settings.load_cases()[:2]
        trials = [{"id": c["id"], "mode": "answer", "repeat": 1} for c in cases]
        class FakeClient:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def verify(self): return settings.identity()
            async def query(self, payload):
                await asyncio.sleep(0)
                return capture()
        args = SimpleNamespace(base_url=settings.BASE_URL, timeout=180, concurrency=2, include_vision=False,
                               top_k=5, hyde=False, selected_formats={"md"})
        settings.RESULTS.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=settings.RESULTS) as directory:
            from pathlib import Path
            with patch.object(run_eval, "AsyncClient", FakeClient), patch.object(run_eval, "RESULTS", Path(directory)), patch("sys.stdout", io.StringIO()):
                self.assertEqual(await run_eval.execute_run(args, cases, trials), 0)
            path = next(Path(directory).glob("*/responses.jsonl"))
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 3)
            self.assertEqual(records[0]["options"]["execution"], "asyncio_httpx")
            self.assertEqual(records[0]["options"]["concurrency"], 2)
            summary = json.loads(path.with_name("run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["completed_trials"], 2)
            self.assertEqual(summary["failed_trials"], 0)
            self.assertTrue(all("client_queue_ms" in row and "history_elapsed_ms" in row for row in records[1:]))

    async def test_independent_cases_overlap_and_respect_concurrency(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        active = peak = 0

        class FakeClient:
            async def query(self, payload):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                if active == 2:
                    entered.set()
                try:
                    await release.wait()
                    return capture()
                finally:
                    active -= 1

        cases = settings.load_cases()[:4]
        trials = [{"id": c["id"], "mode": "answer", "repeat": 1} for c in cases]
        async def collect():
            return [row async for row in run_eval.run_trials(FakeClient(), cases, trials,
                    concurrency=2, top_k=5, use_hyde=False, run_id="overlap")]
        task = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(peak, 2)
            self.assertFalse(task.done())
        finally:
            release.set()
        rows = await asyncio.wait_for(task, 2)
        self.assertEqual(len(rows), 4)
        self.assertLessEqual(peak, 2)
        self.assertEqual(len({r["request"]["session_id"] for r in rows}), 4)
        self.assertTrue(all(r["client_queue_ms"] >= 0 for r in rows))

    async def test_followup_waits_for_previous_done_and_has_fresh_clock(self):
        first_result_seen = asyncio.Event()
        allow_first_done = asyncio.Event()
        requests = []

        class Stream(httpx.AsyncByteStream):
            def __init__(self, first): self.first = first
            async def __aiter__(self):
                yield b'event: delta\ndata: {"text":"answer"}\n\n'
                yield b'event: result\ndata: {"status":"completed","answer":"answer","references":[]}\n\n'
                if self.first:
                    first_result_seen.set()
                    await allow_first_done.wait()
                yield b'event: done\ndata: {}\n\n'

        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream(len(requests) == 1))

        async with AsyncClient(settings.BASE_URL, transport=httpx.MockTransport(handler)) as client:
            with patch("test.async_client.perf_counter", side_effect=[10, 10.5, 20, 30, 30.2, 31]):
                task = asyncio.create_task(run_eval.evaluate_case(client, settings.load_cases()[14], mode="answer",
                                           top_k=5, use_hyde=False, run_id="timing", repeat=1))
                await asyncio.wait_for(first_result_seen.wait(), 2)
                self.assertEqual(len(requests), 1)
                self.assertFalse(task.done())
                allow_first_done.set()
                row = await asyncio.wait_for(task, 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(row["history"][0]["response"]["elapsed_ms"], 10000)
        self.assertEqual(row["response"]["elapsed_ms"], 1000)
        self.assertEqual(row["response"]["first_delta_ms"], 200)
        self.assertEqual(requests[0]["session_id"], requests[1]["session_id"])
        self.assertNotEqual(requests[0]["request_id"], requests[1]["request_id"])

    async def test_async_sse_handles_multiline_crlf(self):
        async def lines():
            for line in [": ping", "event: result\r", 'data: {"status":', 'data: "completed"}', ""]:
                yield line
        self.assertEqual([item async for item in iter_sse(lines())], [("result", {"status": "completed"})])

    async def test_truncated_async_stream_is_failure(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                            content=b'event: result\ndata: {"status":"completed"}\n\n'))
        async with AsyncClient(settings.BASE_URL, transport=transport) as client:
            result = await client.query({"question": "test"})
        self.assertFalse(successful(result))

    async def test_request_total_timeout_closes_stream(self):
        closed = asyncio.Event()
        class Stalled(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b': waiting\n\n'
                await asyncio.Event().wait()
            async def aclose(self): closed.set()
        transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stalled()))
        async with AsyncClient(settings.BASE_URL, timeout=.05, transport=transport) as client:
            result = await client.query({"question": "test"})
        self.assertFalse(successful(result))
        self.assertEqual(result["error"]["exception_type"], "TimeoutError")
        self.assertTrue(closed.is_set())

    async def test_cancellation_cleans_up_active_cases(self):
        entered = asyncio.Event()
        cancelled = 0
        class FakeClient:
            async def query(self, payload):
                nonlocal cancelled
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled += 1
                    raise
        cases = settings.load_cases()[:3]
        trials = [{"id": c["id"], "mode": "answer", "repeat": 1} for c in cases]
        async def collect():
            return [r async for r in run_eval.run_trials(FakeClient(), cases, trials,
                    concurrency=2, top_k=5, use_hyde=False, run_id="cancel")]
        task = asyncio.create_task(collect())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(cancelled, 2)

    async def test_case_exception_does_not_abort_other_cases(self):
        class FakeClient:
            async def query(self, payload):
                if "q01" in payload["session_id"]:
                    raise ValueError("simulated")
                return capture()
        cases = settings.load_cases()[:2]
        trials = [{"id": c["id"], "mode": "answer", "repeat": 1} for c in cases]
        rows = [r async for r in run_eval.run_trials(FakeClient(), cases, trials,
                    concurrency=2, top_k=5, use_hyde=False, run_id="error")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(successful(r["response"]) for r in rows), 1)
        self.assertEqual(next(r for r in rows if r["id"] == "q01")["response"]["error"]["code"], "CASE_EXECUTION_FAILED")

    async def test_out_of_order_completion_remains_scoreable(self):
        cases = settings.load_cases()[:2]
        trials = [{"id": c["id"], "mode": "answer", "repeat": 1} for c in cases]
        manifest = {"type": "run", "schema_version": 1, "run_id": "outoforder", "dataset_sha256": settings.dataset_hash(),
                    "options": {"top_k": 5, "concurrency": 2, "execution": "asyncio_httpx"}, "expected_trials": trials}
        rows = [{"type": "case", **trial, "response": capture(), "client_queue_ms": 2000, "history_elapsed_ms": 9000}
                for trial in reversed(trials)]
        report = score.score_run(manifest, rows, cases)
        self.assertEqual(report["concurrency"], 2)
        self.assertEqual(report["modes"]["answer"]["elapsed_ms_successful"]["p50"], 10)
        self.assertEqual([d["id"] for d in report["details"]], ["q01", "q02"])


if __name__ == "__main__":
    unittest.main()
