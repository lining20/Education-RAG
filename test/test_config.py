"""配置边界与调用方离线验证：假凭据、临时目录、SDK 替身、禁止网络。"""

import importlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from contextlib import contextmanager
import unittest
# 本机 unittest.mock 会经 asyncio 导入 _overlapped 并触发 WinError 10106。
# 这里仅用同步 spy 与属性替换，离线验证无需异步运行时。
class Spy:
    def __init__(self, return_value=None):
        self.calls = []
        self.return_value = return_value
        self.children = {}

    def __call__(self, *args, **kwargs):
        self.calls.append(SimpleNamespace(args=args, kwargs=kwargs))
        if self.return_value is None:
            self.return_value = Spy()
        return self.return_value

    def __getattr__(self, name):
        return self.children.setdefault(name, Spy())

    @property
    def call_args(self):
        return self.calls[-1]

    def assert_called_once(self):
        assert len(self.calls) == 1, f"expected one call, got {len(self.calls)}"

    def assert_not_called(self):
        assert not self.calls, "unexpected SDK call"

    def assert_called_once_with(self, *args, **kwargs):
        self.assert_called_once()
        assert self.call_args.args == args and self.call_args.kwargs == kwargs


class AttributePatch:
    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def start(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self.value

    def stop(self):
        setattr(self.obj, self.name, self.old)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def patch_object(obj, name, value):
    return AttributePatch(obj, name, value)


def patch_target(target, value):
    parts = target.split(".")
    obj = importlib.import_module(parts[0])
    for part in parts[1:-1]:
        obj = getattr(obj, part)
    return AttributePatch(obj, parts[-1], value)


@contextmanager
def patch_dict(mapping, values, clear=False):
    old = mapping.copy()
    if clear:
        mapping.clear()
    mapping.update(values)
    try:
        yield
    finally:
        mapping.clear()
        mapping.update(old)


def blocked(*args, **kwargs):
    raise AssertionError("离线测试禁止网络/DNS")


# 不读取真实 .env，也不继承主机凭据；仅隔离此次配置模块首次导入。
with patch_dict(os.environ, {}, clear=True), patch_target("dotenv.load_dotenv", Spy()):
    from common.config import _env
    from common.config.bailian_mcp_config import McpConfig
    from common.config.embedding_config import EmbeddingConfig
    from common.config.lm_config import LLMConfig
    from common.config.milvus_config import MilvusConfig
    from common.config.minio_config import MinIOConfig
    from common.config.mineru_config import MineruConfig
    from common.config.mongo_config import MongoConfig
    from common.config.reranker_config import RerankerConfig
    from utils.path_util import get_project_root

ROOT = Path(__file__).resolve().parents[1]
# _env 采用 from-import；首次导入隔离结束后恢复真实加载函数供临时 .env 测试使用。
_env.load_dotenv = importlib.import_module("dotenv").load_dotenv


class OfflineTest(unittest.TestCase):
    def setUp(self):
        for target in ("socket.create_connection", "socket.socket.connect", "socket.getaddrinfo"):
            guard = patch_target(target, blocked)
            guard.start()
            self.addCleanup(guard.stop)


class ConfigBoundaryTests(OfflineTest):
    def test_optional_services_do_not_block_default_config(self):
        for cls in (LLMConfig, MinIOConfig, MineruConfig, MongoConfig, MilvusConfig, McpConfig):
            with self.subTest(config=cls.__name__):
                config = cls.from_env({})
                with self.assertRaises(_env.ConfigError):
                    config.validate()
        self.assertEqual(EmbeddingConfig.from_env({}).model_name_or_path, "BAAI/bge-m3")
        self.assertEqual(RerankerConfig.from_env({}).model_name_or_path, "BAAI/bge-reranker-large")

    def test_temperature_missing_blank_zero_and_boundaries(self):
        for raw, expected in ((None, .1), ("", .1), ("  ", .1), ("0", 0), (" 0.0 ", 0), ("2", 2), (".6", .6)):
            with self.subTest(raw=raw):
                self.assertEqual(LLMConfig.from_env({"LLM_DEFAULT_TEMPERATURE": raw}).llm_temperature, expected)
        for raw in ("wrong", "nan", "inf", "-inf", "-0.01", "2.01"):
            with self.subTest(raw=raw), self.assertRaisesRegex(_env.ConfigError, "LLM_DEFAULT_TEMPERATURE"):
                LLMConfig.from_env({"LLM_DEFAULT_TEMPERATURE": raw})

    def test_boolean_tokens_are_shared_by_configs(self):
        for cls, key, attr in ((MinIOConfig, "MINIO_SECURE", "minio_secure"),
                               (EmbeddingConfig, "BGE_FP16", "bge_fp16"),
                               (RerankerConfig, "BGE_RERANKER_FP16", "bge_reranker_fp16")):
            for raw, expected in ((" TRUE ", True), ("1", True), ("Yes", True), ("ON", True),
                                  ("False", False), ("0", False), (" NO ", False), ("off", False),
                                  (None, False), ("", False), ("  ", False)):
                with self.subTest(config=cls.__name__, raw=raw):
                    self.assertIs(getattr(cls.from_env({key: raw}), attr), expected)
            with self.assertRaisesRegex(_env.ConfigError, key):
                cls.from_env({key: "maybe"})

    def test_devices_and_effective_fp16(self):
        for device in ("cpu", "mps", "cuda", "cuda:0", "cuda:12"):
            for cls, prefix in ((EmbeddingConfig, "BGE"), (RerankerConfig, "BGE_RERANKER")):
                config = cls.from_env({prefix + "_DEVICE": device, prefix + "_FP16": "true"})
                self.assertIs(config.use_fp16, device.startswith("cuda"))
        for device in ("auto", "CUDA", "cuda:-1", "cuda:x", "gpu"):
            with self.subTest(device=device), self.assertRaises(_env.ConfigError):
                EmbeddingConfig.from_env({"BGE_DEVICE": device})
            with self.assertRaises(_env.ConfigError):
                RerankerConfig.from_env({"BGE_RERANKER_DEVICE": device})

    def test_model_selection_and_missing_local_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch_object(_env, "PROJECT_ROOT", Path(tmp)):
            local = Path(tmp) / "models" / "local"
            local.mkdir(parents=True)
            self.assertEqual(EmbeddingConfig.from_env({"BGE_M3_PATH": "models/local", "BGE_M3": "org/remote"}).model_name_or_path, str(local.resolve()))
            self.assertEqual(EmbeddingConfig.from_env({"BGE_M3": "org/custom"}).model_name_or_path, "org/custom")
            self.assertEqual(RerankerConfig.from_env({"BGE_RERANKER_PATH": "models/local", "BGE_RERANKER_LARGE": "org/remote"}).model_name_or_path, str(local.resolve()))
            self.assertEqual(RerankerConfig.from_env({"BGE_RERANKER_LARGE": str(local)}).model_name_or_path, str(local.resolve()))
            for cls, key in ((EmbeddingConfig, "BGE_M3_PATH"), (RerankerConfig, "BGE_RERANKER_PATH")):
                for value in ("missing", str(local / "missing")):
                    with self.subTest(key=key), self.assertRaisesRegex(_env.ConfigError, key):
                        _ = cls.from_env({key: value}).model_name_or_path
            for value in ("./missing", "../missing", "C:/missing/model", "https://example.invalid/model"):
                with self.subTest(value=value), self.assertRaises(_env.ConfigError):
                    _ = RerankerConfig.from_env({"BGE_RERANKER_LARGE": value}).model_name_or_path
            file = Path(tmp) / "file"
            file.touch()
            with self.assertRaises(_env.ConfigError):
                _ = EmbeddingConfig.from_env({"BGE_M3_PATH": str(file)}).model_name_or_path

    def test_three_milvus_collections_and_no_old_aliases(self):
        config = MilvusConfig.from_env({"CHUNKS_COLLECTION": "old", "ENTITY_NAME_COLLECTION": "old", "ITEM_NAME_COLLECTION": "old"})
        self.assertEqual((config.courses_collection, config.document_chunks_collection, config.questions_collection),
                         ("education_courses", "education_document_chunks", "education_questions"))
        config = MilvusConfig.from_env({"COURSES_COLLECTION": " c ", "DOCUMENT_CHUNKS_COLLECTION": "d", "QUESTIONS_COLLECTION": "q"})
        self.assertEqual((config.courses_collection, config.document_chunks_collection, config.questions_collection), ("c", "d", "q"))
        for values in ({"COURSES_COLLECTION": "same", "QUESTIONS_COLLECTION": "same"}, {"COURSES_COLLECTION": "bad-name"}):
            with self.assertRaises(_env.ConfigError):
                MilvusConfig.from_env(values)
        self.assertEqual(MilvusConfig.from_env({"COURSES_COLLECTION": "  "}).courses_collection, "education_courses")

    def test_positive_timeouts_and_polling_limits(self):
        for cls, key in ((MilvusConfig, "MILVUS_TIMEOUT"), (MineruConfig, "MINERU_REQUEST_TIMEOUT"),
                         (MineruConfig, "MINERU_POLL_INTERVAL"), (MineruConfig, "MINERU_MAX_POLL_ATTEMPTS"),
                         (MongoConfig, "MONGO_CONNECT_TIMEOUT_MS"), (MongoConfig, "MONGO_SOCKET_TIMEOUT_MS"),
                         (MongoConfig, "MONGO_SERVER_SELECTION_TIMEOUT_MS")):
            for value in ("0", "-1", "NaN", "inf", "bad"):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(_env.ConfigError, key):
                    cls.from_env({key: value})
        with self.assertRaises(_env.ConfigError):
            MineruConfig.from_env({"MINERU_MAX_POLL_ATTEMPTS": "1.5"})

    def test_minio_endpoint_contract(self):
        for endpoint in ("localhost:9000", "storage.example.invalid", "[::1]:9000"):
            MinIOConfig.from_env({"MINIO_ENDPOINT": endpoint, "MINIO_ACCESS_KEY": "dummy",
                                 "MINIO_SECRET_KEY": "dummy", "MINIO_BUCKET_NAME": "education"}).validate()
        for endpoint in ("https://host:9000", "host:9000/path", "user:secret@host", "host:bad", "host:0", "host?x=1"):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(_env.ConfigError, "MINIO_ENDPOINT"):
                MinIOConfig.from_env({"MINIO_ENDPOINT": endpoint, "MINIO_ACCESS_KEY": "dummy",
                                     "MINIO_SECRET_KEY": "dummy", "MINIO_BUCKET_NAME": "education"}).validate()

    def test_mongo_existing_keys_and_seed_lists_without_dns(self):
        for url in ("mongodb://localhost:27017", "mongodb://u:p@db1:27017,db2:27018/?replicaSet=rs0",
                    "mongodb+srv://u:p@cluster.example.invalid/", "mongodb://[::1]:27017"):
            config = MongoConfig.from_env({"MONGO_URL": url, "MONGO_DB_NAME": " education "})
            config.validate()
            self.assertEqual(config.db_name, "education")
            self.assertEqual(config.chat_message_collection, "chat_message")
            self.assertFalse(hasattr(config, "import_tasks_collection"))
            self.assertFalse(hasattr(config, "content_records_collection"))
        for values in ({"MONGO_DB_NAME": "bad/name"}, {"MONGO_URL": "http://localhost"},
                       {"MONGO_URL": "mongodb+srv://host:27017"},
                       {"MONGO_CHAT_MESSAGE_COLLECTION": "system.x"}, {"MONGO_CHAT_MESSAGE_COLLECTION": "bad$name"}):
            env = {"MONGO_URL": "mongodb://localhost:27017", "MONGO_DB_NAME": "education", **values}
            with self.subTest(values=values), self.assertRaises(_env.ConfigError):
                MongoConfig.from_env(env).validate()

    def test_validation_errors_do_not_echo_secrets(self):
        secret = "synthetic-secret-should-not-appear"
        with self.assertRaises(_env.ConfigError) as caught:
            LLMConfig.from_env({"OPENAI_API_KEY": secret, "OPENAI_BASE_URL": "bad://" + secret}).validate()
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(LLMConfig(api_key=secret)))

    def test_dotenv_loading_and_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("LLM_DEFAULT_TEMPERATURE=0\nBGE_FP16=YES\nOPENAI_API_KEY=file-placeholder\n", encoding="utf-8-sig")
            with patch_dict(os.environ, {"OPENAI_API_KEY": "process-placeholder"}, clear=True):
                _env.load_environment(root)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "process-placeholder")
                self.assertEqual(LLMConfig.from_env().llm_temperature, 0)
                self.assertTrue(EmbeddingConfig.from_env().bge_fp16)
            with patch_dict(os.environ, {"OPENAI_API_KEY": ""}, clear=True):
                _env.load_environment(root)
                self.assertIsNone(LLMConfig.from_env().api_key)

    def test_project_root_override_requires_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "file"
            file.touch()
            for value in (str(file), str(Path(tmp) / "absent")):
                with patch_dict(os.environ, {"PROJECT_ROOT": value}), self.assertRaisesRegex(ValueError, "PROJECT_ROOT"):
                    get_project_root()
            with patch_dict(os.environ, {"PROJECT_ROOT": tmp}):
                self.assertEqual(get_project_root(), Path(tmp).resolve())

    def test_clean_subprocess_imports_without_env_or_services(self):
        code = r"""
import os, socket, sys
from pathlib import Path

def blocked(*args, **kwargs):
    raise AssertionError('network/DNS forbidden')
socket.create_connection = socket.getaddrinfo = socket.socket.connect = blocked
sys.path.insert(0, sys.argv[1])
from utils.path_util import PROJECT_ROOT
from common.config.lm_config import lm_config
from common.config.embedding_config import embedding_config
from common.config.reranker_config import reranker_config
from common.config.milvus_config import milvus_config
from common.config.mongo_config import mongo_config
from common.config.minio_config import minio_config
from common.config.mineru_config import mineru_config
from common.config.bailian_mcp_config import mcp_config
assert lm_config.llm_temperature == 0.1
assert lm_config.api_key is None
assert 'pymilvus' not in sys.modules and 'FlagEmbedding' not in sys.modules
assert 'pymongo' not in sys.modules
assert PROJECT_ROOT == Path(os.environ['PROJECT_ROOT']).resolve()
print('isolated import passed')
"""
        with tempfile.TemporaryDirectory() as tmp:
            # 保留 Windows 运行时变量，清除所有项目配置。
            env = {key: value for key, value in os.environ.items() if key.upper() in
                   {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
            env["PROJECT_ROOT"] = tmp
            result = subprocess.run([sys.executable, "-c", code, str(ROOT)], cwd=tmp, env=env,
                                    text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("isolated import passed", result.stdout)


class CallerContractTests(OfflineTest):
    def import_with_stubs(self, name, stubs):
        # 真实项目模块，替换 SDK 和日志；不加载模型、日志 sink 或连接任何服务。
        old = sys.modules.pop(name, None)
        def cleanup():
            sys.modules.pop(name, None)
            if old is not None:
                sys.modules[name] = old
        self.addCleanup(cleanup)
        logger = SimpleNamespace(logger=Spy())
        with patch_dict(sys.modules, {"common.logging.logger": logger, **stubs}):
            return importlib.import_module(name)

    def test_llm_zero_reaches_sdk_and_cache(self):
        sdk = Spy(return_value=object())
        module = self.import_with_stubs("utils.lm.lm_utils", {
            "langchain_openai": SimpleNamespace(ChatOpenAI=sdk),
            "langchain_core.exceptions": SimpleNamespace(LangChainException=Exception),
        })
        module.lm_config = LLMConfig.from_env({"OPENAI_BASE_URL": "https://example.invalid/v1",
                                             "OPENAI_API_KEY": "dummy", "LLM_DEFAULT_TEMPERATURE": "0"})
        first = module.get_llm_client()
        self.assertIs(first, module.get_llm_client())
        self.assertEqual(sdk.call_args.kwargs["temperature"], 0)
        sdk.assert_called_once()
        module._llm_client_cache.clear()
        module.lm_config = LLMConfig.from_env({"OPENAI_API_KEY": "dummy"})
        with self.assertRaisesRegex(_env.ConfigError, "OPENAI_BASE_URL"):
            module.get_llm_client()

    def test_embedding_config_reaches_sdk_and_bad_path_stops_sdk(self):
        sdk = Spy(return_value=object())
        module = self.import_with_stubs("utils.lm.embedding_utils", {
            "pymilvus.model.hybrid": SimpleNamespace(BGEM3EmbeddingFunction=sdk),
        })
        module.embedding_config = EmbeddingConfig.from_env({"BGE_M3": "org/custom", "BGE_FP16": "true", "BGE_DEVICE": "cpu"})
        module.get_bge_m3_ef()
        self.assertEqual(sdk.call_args.kwargs["model_name"], "org/custom")
        self.assertFalse(sdk.call_args.kwargs["use_fp16"])
        module._bge_m3_ef = None
        module.embedding_config = EmbeddingConfig(bge_m3_path="./missing-offline-test-model")
        with self.assertRaises(_env.ConfigError):
            module.get_bge_m3_ef()
        sdk.assert_called_once()

    def test_reranker_config_reaches_sdk(self):
        sdk = Spy(return_value=object())
        # 已安装 FlagEmbedding 的构造参数是 devices；严格签名避免 device 被 **kwargs 静默忽略。
        def factory(model_name_or_path, devices, use_fp16):
            return sdk(model_name_or_path=model_name_or_path, devices=devices, use_fp16=use_fp16)
        module = self.import_with_stubs("utils.lm.reranker_utils", {"FlagEmbedding": SimpleNamespace(FlagReranker=factory)})
        module.reranker_config = RerankerConfig.from_env({"BGE_RERANKER_LARGE": "org/reranker",
                                                        "BGE_RERANKER_DEVICE": "cuda:1", "BGE_RERANKER_FP16": "yes"})
        module.get_reranker_model()
        self.assertEqual(sdk.call_args.kwargs, {"model_name_or_path": "org/reranker", "devices": "cuda:1", "use_fp16": True})

    def test_minio_invalid_config_prevents_client_creation(self):
        sdk = Spy()
        module = self.import_with_stubs("utils.clients.minio_utils", {"minio": SimpleNamespace(Minio=sdk)})
        module.minio_config = MinIOConfig.from_env({})
        with self.assertRaisesRegex(_env.ConfigError, "MINIO_ENDPOINT"):
            module._create_minio_client()
        sdk.assert_not_called()

    def test_milvus_timeout_reaches_client_without_collection_creation(self):
        sdk = Spy()
        module = self.import_with_stubs("utils.clients.milvus_utils", {"pymilvus": SimpleNamespace(
            MilvusClient=sdk, AnnSearchRequest=Spy(), RRFRanker=Spy(), WeightedRanker=Spy())})
        module.milvus_config = MilvusConfig.from_env({"MILVUS_URL": "http://localhost:19530", "MILVUS_TIMEOUT": "2.5"})
        module.get_milvus_client()
        sdk.assert_called_once_with(uri="http://localhost:19530", timeout=2.5)
        sdk.return_value.create_collection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
