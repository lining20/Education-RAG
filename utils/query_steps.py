"""查询节点的业务步骤，复用模型、历史、向量检索与重排工具。"""
import json
import math
import re
from common.errors import OperationError, error_summary
from common.logging.logger import logger, step_log
from utils.query_execution import run_sync
from utils.query_retrieval import reciprocal_rank_fusion


@step_log("query_step_validate")
async def validate(state, deps):
    if not state["question"].strip():
        raise OperationError("EMPTY_QUESTION", "请输入要检索的问题")
    return {}


@step_log("query_step_history")
async def history(state, deps):
    messages = await run_sync(deps.history.recent_messages, state["session_id"], 10)
    messages = [{"role": m["role"], "text": m["text"][:4000]} for m in messages
                if m.get("request_id") != state["request_id"] and m.get("status") not in {"failed", "interrupted"}]
    options = {key: state[key] for key in ("mode", "scope", "filters", "top_k", "use_hyde", "use_web")}
    options["is_stream"] = state.get("is_stream", True)
    await run_sync(deps.history.save_message, state["session_id"], "user", state["question"],
                   request_id=state["request_id"], result={"request_options": options})
    return {"history": messages}


@step_log("query_step_rewrite")
async def rewrite(state, deps):
    raw = await deps.llm.complete("query_rewrite", question=state["question"], history=state["history"])
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        question = data["rewritten_query"]
        subject = data.get("subject_name", "")
        if not isinstance(question, str) or not question.strip() or not isinstance(subject, str):
            raise ValueError("invalid rewrite")
        clarification = data.get("clarification", "") if data.get("needs_clarification") is True else ""
        if not isinstance(clarification, str):
            raise ValueError("invalid clarification")
        return {"rewritten_query": question.strip()[:2000], "subject_name": subject[:512], "clarification": clarification[:1000]}
    except (ValueError, KeyError, TypeError):
        return {"rewritten_query": state["question"], "subject_name": "",
                "warnings": [{"code": "REWRITE_INVALID", "message": "问题改写结果无效，使用原问题检索"}]}


@step_log("query_step_subjects")
async def subjects(state, deps):
    texts = [state["rewritten_query"]] + ([state["subject_name"]] if state["subject_name"] else [])
    vectors = await run_sync(deps.embed, texts)
    query_vectors = {k: [v[0]] for k, v in vectors.items()}
    ids = []
    if state["subject_name"]:
        ids = await run_sync(deps.retrieval.subjects, {k: [v[1]] for k, v in vectors.items()}, state["subject_name"])
    logger.info("主体确认：名称={}，排序参考文档数={}；不限制后续本地召回范围", state["subject_name"], len(ids))
    return {"vectors": query_vectors, "document_ids": ids}


@step_log("query_step_vector_search")
async def vector_search(state, deps):
    rows = await run_sync(deps.retrieval.retrieve, state["vectors"], state["scope"], state["filters"], state["document_ids"])
    return {"vector_hits": rows}


@step_log("query_step_hyde_search")
async def hyde_search(state, deps):
    if not state["use_hyde"]:
        return {"hyde_hits": []}
    try:
        hypothesis = await deps.llm.complete("query_hyde", question=state["rewritten_query"])
        vectors = await run_sync(deps.embed, [hypothesis[:2000]])
        rows = await run_sync(deps.retrieval.retrieve, vectors, state["scope"], state["filters"], state["document_ids"])
        return {"hyde_hits": rows}  # 假设文字永不作为候选证据。
    except Exception as exc:
        code, _, _ = error_summary(exc)
        return {"hyde_hits": [], "warnings": [{"code": code, "message": "HyDE 不可用，使用其他召回结果"}]}


@step_log("query_step_web_search")
async def web_search(state, deps):
    if not state["use_web"]:
        return {"web_hits": []}
    try:
        return {"web_hits": await deps.web_search(state["rewritten_query"])}
    except Exception as exc:
        code, message, _ = error_summary(exc)
        return {"web_hits": [], "warnings": [{"code": code, "message": message}]}


@step_log("query_step_join_rrf")
async def join_rrf(state, deps):
    candidates = reciprocal_rank_fusion([state["vector_hits"], state["hyde_hits"], state["web_hits"]])
    logger.info("多路召回融合：本地混合={}，本地HyDE={}，网页={}，融合候选={}",
                len(state["vector_hits"]), len(state["hyde_hits"]), len(state["web_hits"]), len(candidates))
    return {"candidates": candidates}


@step_log("query_step_rerank")
async def rerank(state, deps):
    candidates = state["candidates"]
    if not candidates:
        return {"references": []}
    scores = await run_sync(deps.rerank, state["rewritten_query"], [row["content"][:6000] for row in candidates])
    if len(scores) != len(candidates) or any(not math.isfinite(float(s)) for s in scores):
        raise OperationError("INVALID_RERANK_RESULT", "重排序返回了无效分数")
    local, web, seen = [], [], set()
    # 相关性分数始终优先；仅同分时参考主体命中和 RRF，不修改真实模型分数。
    ranked = sorted(zip(scores, candidates), key=lambda pair: (
        float(pair[0]), bool(pair[1].get("subject_match")), pair[1].get("rrf_score", 0)), reverse=True)
    for score, candidate in ranked:
        key = (candidate.get("collection"), candidate.get("row", {}).get("record_id", candidate["key"]))
        if score >= .15 and key not in seen:
            target = web if candidate.get("kind") == "web" else local
            target.append({**candidate, "rerank_score": float(score)})
            seen.add(key)
    # 来源各自保持真实分数排序。联网只补充，不让网页挤掉所有合格的本地证据。
    limit = state["top_k"]
    local_quota = (limit * 3 + 4) // 5  # 优先保留 60% 本地：3→2、5→3、10→6。
    ordered = local[:local_quota]
    ordered.extend(web[:limit - len(ordered)])
    ordered.extend(local[local_quota:local_quota + limit - len(ordered)])
    references = await run_sync(deps.retrieval.references, ordered, state["session_id"], state["request_id"])
    local_count = sum(ref.get("kind") != "web" for ref in references)
    logger.info("重排来源保留：阈值=0.15，合格本地={}，合格网页={}，最终本地={}，最终网页={}",
                len(local), len(web), local_count, len(references) - local_count)
    update = {"references": references}
    if state.get("use_web") and references and not local_count:
        update["warnings"] = [{"code": "LOCAL_EVIDENCE_NOT_FOUND",
            "message": "本轮未找到可用且达到相关性阈值的本地资料，当前回答参考联网结果。"}]
    return update


@step_log("query_step_answer")
async def answer(state, deps):
    if state.get("clarification"):
        return {"answer": state["clarification"], "status": "clarification_required"}
    # 生成前再次核验清单，防止召回期间同名重传使旧证据失效。
    refs = await run_sync(deps.retrieval.references, state["references"], state["session_id"], state["request_id"])
    deps.progress.references = refs
    await deps.emit("sources", {"references": refs, "rewritten_query": state["rewritten_query"]})
    if not refs:
        return {"references": [], "answer": "当前资料中未找到足够相关的证据，请补充课程、项目或知识点后重试。",
                "status": "insufficient_evidence" if state.get("candidates") else "no_results"}
    if state["mode"] == "search":
        return {"references": refs, "answer": f"找到 {len(refs)} 条相关资料，可展开查看完整内容和来源。", "status": "completed"}
    evidence = []
    for ref in refs:
        payload = ref.get("record", {}).get("payload", {})
        evidence.append({"id": ref["id"], "kind": ref["kind"], "title": ref["title"], "text": ref["content"][:5000],
                         "images": ref.get("images", []),
                         "original_answer": str(payload.get("answer", ""))[:4000],
                         "original_explanation": str(payload.get("explanation", ""))[:4000]})
    variables = {"question": state["rewritten_query"], "evidence": evidence, "history": state["history"]}
    if state.get("is_stream", True):
        chunks = []
        async for text in deps.llm.stream(**variables):
            chunks.append(text)
            deps.progress.partial_answer = "".join(chunks)
            await deps.emit("delta", {"text": text})
        text = "".join(chunks).strip()
    else:
        # 非流式调用只返回完整回答；流程进度仍通过 SSE 更新。
        text = (await deps.llm.complete("query_answer", **variables)).strip()
    if not text:
        raise OperationError("EMPTY_ANSWER", "模型未生成回答，请稍后重试", retryable=True)
    # 模型只能选择本轮切片关联的原图；地址由现有代理根据 MinIO object_key 读取。
    body, separator, image_block = text.partition("【图片】")
    if separator:
        allowed_images = {image["url"] for ref in refs if ref.get("kind") != "web"
                          for image in ref.get("images", [])}
        requested = [line.strip() for line in image_block.splitlines() if line.strip()]
        selected = list(dict.fromkeys(url for url in requested if url in allowed_images))
        rejected = sum(url not in allowed_images for url in requested)
        if rejected:
            logger.warning("答案配图核验：移除不属于本轮引用的图片地址数={}", rejected)
        logger.info("答案配图核验：候选原图数={}，选中原图数={}", len(allowed_images), len(selected))
        text = body.rstrip() + ("\n\n【图片】\n" + "\n".join(selected) if selected else "")
    # 只核验正文引用；图片区块、代码、Markdown 图片和链接地址不是证据编号。
    from markdown_it import MarkdownIt
    citation_ids = set()
    for block in MarkdownIt("commonmark").enable("table").parse(text.split("【图片】", 1)[0]):
        for token in block.children or []:
            if token.type == "text":
                citation_ids.update(re.findall(r"\[(\d+)\]", token.content))
    invalid_ids = citation_ids - {str(ref["id"]) for ref in refs}
    if invalid_ids:
        logger.warning("回答引用校验失败：本次有效编号={}，无效编号={}",
                       [str(ref["id"]) for ref in refs], sorted(invalid_ids))
        return {"references": refs, "answer": "生成回答包含无法核验的引用，请查看来源资料或重新提问。", "status": "insufficient_evidence"}
    return {"references": refs, "answer": text, "status": "completed"}


def compact_result(result):
    """Mongo 只保存对话和引用定位，不复制完整课程/题目业务记录。"""
    saved = {**result, "references": []}
    for reference in result.get("references", []):
        ref = {k: v for k, v in reference.items() if k != "record"}
        if "row" in ref:
            ref["row"] = {k: v for k, v in ref["row"].items() if k not in {"record_payload", "content"}}
        ref["content"] = ref.get("content", "")[:1000]
        saved["references"].append(ref)
    return saved


@step_log("query_step_save_history")
async def save_history(state, deps):
    result = {"session_id": state["session_id"], "request_id": state["request_id"], "status": state["status"],
              "answer": state["answer"], "rewritten_query": state["rewritten_query"],
              "references": state["references"], "warnings": state["warnings"]}
    await run_sync(deps.history.save_message, state["session_id"], "assistant", state["answer"],
        request_id=state["request_id"], rewritten_query=state["rewritten_query"], status=state["status"],
        references=compact_result(result)["references"], result=compact_result(result))
    return {"result": result}


@step_log("query_step_handle_error")
async def handle_error(state, deps):
    error = state["errors"][0]
    try:
        await run_sync(deps.history.save_message, state["session_id"], "assistant", error["message"],
            request_id=state["request_id"], status="failed", result={"status": "failed", "error": error})
    except Exception:
        pass  # 历史服务故障不能掩盖首个业务错误。
    return {"status": "failed", "result": {"status": "failed", "error": error}}
