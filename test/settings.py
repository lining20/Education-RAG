"""测试路径、样本校验和显式启动入口。仅使用标准库即可离线校验。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DOCUMENTS = ROOT / "data" / "documents"
QUESTIONS = ROOT / "data" / "questions.jsonl"
RESULTS = ROOT / "results"
SUPPORTED_FORMATS = {".md", ".docx", ".pdf"}
BASE_URL = os.environ.get("RAG_TEST_BASE_URL", "http://127.0.0.1:8201").rstrip("/")
ISOLATION = {
    "DOCUMENT_INDEX_COLLECTION": "rag_test_v1_document_index",
    "COURSES_COLLECTION": "rag_test_v1_courses",
    "DOCUMENT_CHUNKS_COLLECTION": "rag_test_v1_document_chunks",
    "QUESTIONS_COLLECTION": "rag_test_v1_questions",
    "MINIO_BUCKET_NAME": "rag-test-v1",
    "MONGO_DB_NAME": "rag_test_v1",
    "MONGO_CHAT_MESSAGE_COLLECTION": "chat_message",
}


def normalize(value):
    return re.sub(r"\s+", "", str(value)).casefold()


def inside(path, root=ROOT):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"路径必须在 {root} 内")
    return resolved


def data_directory(version=None):
    if version not in {None, "current", "v1"}:
        raise ValueError("未知数据版本")
    return ROOT / "data" if version in {None, "current"} else ROOT / "data" / "versions" / "v1"


def list_documents(version=None):
    return sorted(path for path in (data_directory(version) / "documents").iterdir()
                  if path.is_file() and path.suffix.lower() in SUPPORTED_FORMATS)


def source_text(name, version=None):
    base = data_directory(version)
    path = inside(base / "documents" / name, base / "documents")
    if path.suffix == ".md":
        return path.read_text(encoding="utf-8-sig")
    if path.suffix == ".docx":
        with ZipFile(path) as archive:
            tree = ET.fromstring(archive.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        return "\n".join("".join(t.text or "" for t in p.findall(".//w:t", ns)) for p in tree.findall(".//w:p", ns))
    if path.suffix == ".pdf":
        # 由生成器使用 pypdf 从真实 PDF 提取，散列确保侧记与 PDF 一致。
        sources = json.loads((base / "evidence_sources.json").read_text(encoding="utf-8"))
        item = sources[name]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("PDF 已变化，请重新生成证据提取侧记")
        return item["text"]
    raise ValueError("不支持的证据格式")


def load_cases(version=None):
    base = data_directory(version)
    cases = [json.loads(line) for line in (base / "questions.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    seen = set()
    for case in cases:
        cid = case["id"]
        if not re.fullmatch(r"q\d{2,}", cid) or cid in seen:
            raise ValueError(f"问题 ID 非法或重复：{cid}")
        seen.add(cid)
        if not case["question"].strip() or not case["expected_answer"].strip():
            raise ValueError(f"空问题或答案：{cid}")
        if type(case["answerable"]) is not bool:
            raise ValueError(f"answerable 必须为布尔值：{cid}")
        if bool(case["evidence"]) != case["answerable"]:
            raise ValueError(f"证据与可回答性不一致：{cid}")
        if case["answerable"] and not case["answer_checks"]:
            raise ValueError(f"有答案的问题缺少要点检查：{cid}")
        for check in case["answer_checks"]:
            if not check["any_of"] or not any(normalize(v) in normalize(case["expected_answer"]) for v in check["any_of"]):
                raise ValueError(f"标准答案未覆盖检查项：{cid}/{check['label']}")
        for evidence in case["evidence"]:
            path = inside(base / "documents" / evidence["file"], base / "documents")
            if path.suffix not in SUPPORTED_FORMATS or not evidence["quote"].strip():
                raise ValueError(f"证据格式不正确：{cid}")
            text = source_text(evidence["file"], version)
            if normalize(evidence["heading"]) not in normalize(text):
                raise ValueError(f"证据无法回溯原文：{cid}/{path.name}")
            if evidence.get("origin") == "image":
                asset = inside(base / evidence["asset"], base / "assets")
                sources = json.loads((base / "evidence_sources.json").read_text(encoding="utf-8"))
                item = sources[evidence["asset"]]
                if hashlib.sha256(asset.read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError(f"图片与证据标注不一致：{cid}")
                text = item["text"]
                if evidence.get("match_mode") != "manual_image" or "vision" not in case.get("requires", []):
                    raise ValueError(f"图片题必须单独标记视觉能力与人工核验：{cid}")
            if normalize(evidence["quote"]) not in normalize(text):
                raise ValueError(f"证据摘录无法回溯：{cid}/{path.name}")
    if not cases:
        raise ValueError("问题集为空")
    return cases


def dataset_hash(version=None):
    digest = hashlib.sha256()
    base = data_directory(version)
    paths = [base / "questions.jsonl", *list_documents(version)]
    if (base / "assets").exists():
        paths.extend(sorted((base / "assets").glob("*.png")))
    if (base / "evidence_sources.json").exists():
        paths.append(base / "evidence_sources.json")
    for path in paths:
        digest.update(("data/" + path.relative_to(base).as_posix()).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def identity():
    return {"suite": "local-rag-test-v2", "dataset_sha256": dataset_hash(), "isolation": ISOLATION}


def validate_base_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("评测服务仅允许本机 HTTP 地址，不能包含凭据、路径或查询参数")
    return url.rstrip("/")


def apply_test_environment():
    # 必须在导入任何业务模块之前调用；.env 仍提供服务连接与模型配置。
    if "api.main" in sys.modules or "common.config._env" in sys.modules:
        raise RuntimeError("业务配置已经加载，请使用新的进程启动测试服务")
    os.environ.update(ISOLATION)
    os.environ.update({"LOG_FILE_ENABLE": "False", "IMAGE_UNDERSTANDING_ENABLED": os.environ.get("RAG_TEST_IMAGES", "False"),
                       "PYTHONDONTWRITEBYTECODE": "1", "TMP": str(RESULTS / "tmp"),
                       "TEMP": str(RESULTS / "tmp"), "TMPDIR": str(RESULTS / "tmp")})
    sys.dont_write_bytecode = True
    (RESULTS / "tmp").mkdir(parents=True, exist_ok=True)
    import tempfile
    tempfile.tempdir = str(RESULTS / "tmp")


def create_test_app():
    apply_test_environment()
    from api.main import ImportService, create_app
    app = create_app(service=ImportService(upload_root=RESULTS / "uploads"))
    startup_identity = identity()
    from common.config.lm_config import lm_config
    startup_identity["capabilities"] = {"image_understanding_enabled": lm_config.image_understanding_enabled}

    @app.get("/api/test/identity", include_in_schema=False)
    async def test_identity():
        return startup_identity

    return app


def main():
    parser = argparse.ArgumentParser(description="默认只校验本地数据；显式选项才启动服务/创建账号。")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--serve", action="store_true")
    action.add_argument("--create-user", action="store_true")
    parser.add_argument("--port", type=int, default=8201)
    parser.add_argument("--images", action="store_true", help="启动服务时开启真实图片理解，导入可能调用视觉模型")
    args = parser.parse_args()
    cases = load_cases()
    if args.serve:
        os.environ["RAG_TEST_IMAGES"] = str(args.images)
        import uvicorn
        uvicorn.run(create_test_app(), host="127.0.0.1", port=args.port, workers=1)
    elif args.create_user:
        apply_test_environment()
        from scripts.create_upload_user import main as create_user
        create_user()
    else:
        print(json.dumps({**identity(), "documents": len(list_documents()),
                          "cases": len(cases), "answerable": sum(c['answerable'] for c in cases),
                          "vision_cases": sum("vision" in c.get("requires", []) for c in cases),
                          "network": "未连接"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
