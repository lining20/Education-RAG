"""批量调用真实 SSE 接口。默认仅验证本地问题；--execute 才调用模型并写入测试会话库。"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from test.async_client import AsyncClient
from test.http_client import successful
from test.settings import BASE_URL, RESULTS, dataset_hash, load_cases


async def evaluate_case(client, case, *, mode, top_k, use_hyde, run_id, repeat):
    session_id = f"rt_{run_id}_{mode}_{case['id']}_{repeat}"
    base = {"session_id": session_id, "mode": mode, "scope": "document_chunk", "top_k": top_k,
            "use_hyde": use_hyde, "use_web": False, "is_stream": True, "filters": {}}
    history = []
    for index, question in enumerate(case["history"]):
        # 用系统真实回答建立上下文；不把标准答案喂给系统。
        request = {**base, "request_id": uuid4().hex, "mode": "answer", "question": question}
        response = await client.query(request)
        history.append({"request": request, "response": response})
        if not successful(response) or response["result"]["status"] != "completed":
            return {"type": "case", "id": case["id"], "mode": mode, "repeat": repeat,
                    "history": history, "request": None, "response": {"done": False, "result": None,
                    "error": {"code": "HISTORY_SETUP_FAILED"}, "elapsed_ms": None, "first_delta_ms": None}}
    request = {**base, "request_id": uuid4().hex, "question": case["question"]}
    return {"type": "case", "id": case["id"], "mode": mode, "repeat": repeat,
            "history": history, "request": request, "response": await client.query(request)}


async def run_trials(client, cases, trials, *, concurrency, top_k, use_hyde, run_id):
    """独立案例并发；结果由调用方单点写盘，避免并发写乱JSONL。"""
    slots = asyncio.Semaphore(concurrency)
    by_id = {case["id"]: case for case in cases}

    async def execute(trial):
        queued = perf_counter()
        async with slots:
            started = perf_counter()
            try:
                row = await evaluate_case(client, by_id[trial["id"]], mode=trial["mode"], repeat=trial["repeat"],
                                          top_k=top_k, use_hyde=use_hyde, run_id=run_id)
            except Exception as exc:
                row = {"type": "case", **trial, "history": [], "request": None,
                       "response": {"done": False, "result": None, "elapsed_ms": None, "first_delta_ms": None,
                                    "error": {"code": "CASE_EXECUTION_FAILED", "exception_type": type(exc).__name__}}}
            row["client_queue_ms"] = round((started - queued) * 1000, 3)
            row["case_elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
            row["history_elapsed_ms"] = round(sum(h["response"].get("elapsed_ms") or 0 for h in row["history"]), 3)
            return row

    tasks = [asyncio.create_task(execute(trial)) for trial in trials]
    try:
        for completed in asyncio.as_completed(tasks):
            yield await completed
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def execute_run(args, cases, trials):
    async with AsyncClient(args.base_url, args.timeout, args.concurrency) as client:
        server_identity = await client.verify()
        if args.include_vision and not server_identity.get("capabilities", {}).get("image_understanding_enabled"):
            raise ValueError("视觉题需要使用 test.settings --serve --images 启动服务，并在开启后重新导入")
        run_id = uuid4().hex[:12]
        directory = RESULTS / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + run_id)
        directory.mkdir(parents=True)
        path = directory / "responses.jsonl"
        manifest = {"type": "run", "schema_version": 1, "run_id": run_id,
                    "started_at": datetime.now(timezone.utc).isoformat(), "dataset_sha256": dataset_hash(),
                    "server": server_identity, "base_url": args.base_url,
                    "options": {"top_k": args.top_k, "use_hyde": args.hyde, "use_web": False,
                                "include_vision": args.include_vision, "formats": sorted(args.selected_formats),
                                "concurrency": args.concurrency, "execution": "asyncio_httpx"},
                    "expected_trials": trials,
                    "timing": "target request to SSE done; excludes client slot wait and completed history; includes HTTP pool/service queue and model cold start"}
        failed = completed = 0
        started = perf_counter()
        print("按完成顺序保存到：", path)
        with path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(manifest, ensure_ascii=False) + "\n")
            output.flush()
            async for row in run_trials(client, cases, trials, concurrency=args.concurrency,
                                        top_k=args.top_k, use_hyde=args.hyde, run_id=run_id):
                failed += not successful(row["response"])
                completed += 1
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                print(f"[{completed}/{len(trials)}]", row["mode"], row["id"],
                      "完成" if successful(row["response"]) else "失败")
        elapsed = perf_counter() - started
        summary = {"run_id": run_id, "concurrency": args.concurrency, "completed_trials": completed,
                   "failed_trials": failed, "run_elapsed_seconds": round(elapsed, 3),
                   "target_trials_per_second": round(completed / elapsed, 4) if elapsed else None,
                   "timing_note": "整轮耗时含前置对话；目标查询P50/P95不含客户端等待名额和前置问答"}
        (directory / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("评测结果：", path)
        print("评分命令：python -B -m test.score --input", path)
        return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--mode", choices=["search", "answer", "both"], default="both")
    parser.add_argument("--top-k", type=int, choices=range(1, 11), default=5)
    parser.add_argument("--hyde", action="store_true")
    parser.add_argument("--include-vision", action="store_true", help="加入图中独有信息的问题，需要 --images 服务且已重新导入")
    parser.add_argument("--formats", default="md,docx,pdf", help="仅评测全部证据来自指定格式的案例，例如 md 或 md,docx")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=4, help="同时执行的独立案例数量，默认4，最多256")
    parser.add_argument("--timeout", type=float, default=180, help="每次HTTP请求到done的总超时，单位秒，不含客户端名额等待与前置问答")
    args = parser.parse_args()
    if args.repeat < 1 or args.timeout <= 0 or (args.limit is not None and args.limit < 1):
        parser.error("repeat、limit、timeout 必须为正数")
    if not 1 <= args.concurrency <= 256:
        parser.error("concurrency 必须为1到256之间的整数")
    formats = {value.strip().lower() for value in args.formats.split(",")}
    if not formats or not formats <= {"md", "docx", "pdf"}:
        parser.error("formats 只允许 md、docx、pdf，以逗号分隔")
    args.selected_formats = formats
    all_cases = load_cases()
    cases = [case for case in all_cases if (args.include_vision or "vision" not in case.get("requires", []))
             and set(case.get("source_formats") or {Path(e["file"]).suffix[1:] for e in case["evidence"]} or {"md"}) <= formats][:args.limit]
    if not cases:
        parser.error("没有符合所选格式与能力的案例")
    modes = ["search", "answer"] if args.mode == "both" else [args.mode]
    trials = [{"id": case["id"], "mode": mode, "repeat": repeat}
              for mode in modes for repeat in range(1, args.repeat + 1) for case in cases]
    print(f"本地样本有效：{len(cases)} 个案例，{len(trials)} 次目标查询；多轮案例另有前置真实问答。")
    print(f"asyncio 协程并发：{args.concurrency}；本题响应计时不包含前置问答和客户端名额等待。")
    if not args.execute:
        print("仅本地校验；未连接服务、未调用模型、未写会话。加 --execute 才执行。")
        return 0
    return asyncio.run(execute_run(args, cases, trials))


if __name__ == "__main__":
    raise SystemExit(main())
