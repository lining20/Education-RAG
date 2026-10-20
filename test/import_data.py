"""默认仅展示导入计划。--execute 才登录、上传并等待真实导入。"""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from time import monotonic, sleep
from uuid import uuid4

from test.http_client import Client
from test.settings import BASE_URL, RESULTS, dataset_hash, list_documents, load_cases


def multipart(documents):
    boundary = "ragtest" + uuid4().hex
    parts = []
    for key, value in {"content_type": "document_chunk"}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for path in documents:
        mime = {".md": "text/markdown", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".pdf": "application/pdf"}[path.suffix.lower()]
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                      f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n').encode("utf-8"))
        parts.extend([path.read_bytes(), b"\r\n"])
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="实际写入隔离测试库；同名文件会替换")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--wait-seconds", type=float, default=600)
    args = parser.parse_args()
    if args.wait_seconds <= 0:
        parser.error("--wait-seconds 必须大于零")
    cases = load_cases()
    documents = list_documents()
    plan = {"execute": args.execute, "base_url": args.base_url, "dataset_sha256": dataset_hash(),
            "content_type": "document_chunk", "documents": [p.name for p in documents],
            "questions_not_uploaded": len(cases)}
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.execute:
        print("仅本地校验；未登录、未连接服务、未导入。")
        return 0
    client = Client(args.base_url)
    client.verify()
    username = os.environ.get("RAG_TEST_USERNAME") or input("测试上传账号：").strip()
    password = os.environ.get("RAG_TEST_PASSWORD") or getpass.getpass("测试上传密码：")
    client.json("/api/auth/login", {"username": username, "password": password})
    del password
    body, content_type = multipart(documents)
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / ("import_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8] + ".json")
    report = {**plan, "started_at": datetime.now(timezone.utc).isoformat()}
    code = 1
    try:
        with client.open("/api/import/tasks", method="POST", data=body, content_type=content_type) as response:
            submitted = json.load(response)
        report["submitted"] = submitted
        print("批次已提交：", submitted["batch_id"])
        deadline = monotonic() + args.wait_seconds
        while True:
            batch = client.json("/api/import/batches/" + submitted["batch_id"])
            report["last_batch"] = batch
            statuses = [task["status"] for task in batch["tasks"]]
            if statuses and all(status in {"succeeded", "failed"} for status in statuses):
                code = 0 if all(status == "succeeded" for status in statuses) else 1
                break
            if monotonic() >= deadline:
                report["wait_timed_out"] = True
                print("等待超时；后台任务可能仍在运行，请用批次编号检查，避免立即重传。")
                break
            sleep(min(1, max(0, deadline - monotonic())))
    except Exception as exc:
        report["client_error_type"] = type(exc).__name__
        print("导入请求或查询失败，检查测试服务终端。已提交任务可能继续运行。")
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        with path.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
        print("导入报告：", path)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
