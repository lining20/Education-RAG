"""MongoDB 只保存会话历史，按 session_id 隔离；导入本模块不连接数据库。

同步 PyMongo 方法供 Service/查询工具在线程中调用。索引由应用显式初始化；
课程、题目、任务和来源清单均不在此存储。
"""
from copy import deepcopy
from threading import Lock
from time import time_ns
from uuid import NAMESPACE_URL, uuid5
import json

from common.config.mongo_config import MongoConfig, mongo_config
from common.errors import OperationError
from utils.clients.mongo_utils import create_mongo_client
from utils.task_utils import validate_identifier


class HistoryMongoTool:
    def __init__(self, *, client=None, config: MongoConfig = mongo_config, collection=None):
        """可注入 Collection 做离线验证；共享客户端的关闭由调用方负责。"""
        if collection is not None:
            self.client = client
            self.chat_message = collection
        else:
            config.validate()
            self.client = client if client is not None else create_mongo_client(config)
            self.chat_message = self.client[config.db_name][config.chat_message_collection]

    def ensure_indexes(self) -> None:
        """应用启动阶段显式调用；不会在模块导入或普通读取时自动建索引。"""
        self.chat_message.create_index([("session_id", 1), ("ts", -1), ("_id", -1)])

    def save_message(self, session_id: str, role: str, text: str, *, request_id: str,
                     rewritten_query: str = "", status: str = "completed",
                     references: list | None = None, result: dict | None = None,
                     image_urls: list | None = None) -> str:
        """同一 session/request/role 使用稳定 ID；重试不会重复插入用户/助手消息。"""
        validate_identifier(session_id, "session_id")
        validate_identifier(request_id, "request_id")
        if role not in {"user", "assistant"} or not isinstance(text, str):
            raise OperationError("INVALID_HISTORY_MESSAGE", "消息角色或正文类型不合法")
        if status not in {"completed", "clarification_required", "no_results", "insufficient_evidence", "failed", "interrupted"}:
            raise OperationError("INVALID_HISTORY_MESSAGE", "历史消息状态不合法")
        key = str(uuid5(NAMESPACE_URL, json.dumps([session_id, request_id, role], ensure_ascii=True)))
        document = {
            "session_id": session_id, "request_id": request_id, "role": role, "text": text,
            "rewritten_query": rewritten_query, "status": status,
            "references": deepcopy(references or []), "result": deepcopy(result or {}),
            "image_urls": list(image_urls or []),
        }
        try:
            self.chat_message.update_one(
                {"_id": key, "session_id": session_id},
                # 内部排序用纳秒，避免同毫秒内 user/assistant 被 UUID 排序颠倒。
                {"$set": document, "$setOnInsert": {"ts": time_ns()}}, upsert=True,
            )
        except Exception as exc:
            raise OperationError("HISTORY_STORE_UNAVAILABLE", "会话消息保存失败", retryable=True) from exc
        return key

    def recent_messages(self, session_id: str, limit: int = 10) -> list[dict]:
        """先取最近 N 条，再反转为时间正序；空历史与数据库失败明确区分。"""
        validate_identifier(session_id, "session_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise OperationError("INVALID_HISTORY_LIMIT", "历史条数必须为 1 到 100 的整数")
        try:
            rows = list(self.chat_message.find({"session_id": session_id}).sort([("ts", -1), ("_id", -1)]).limit(limit))
        except Exception as exc:
            raise OperationError("HISTORY_STORE_UNAVAILABLE", "会话历史读取失败", retryable=True) from exc
        return list(reversed(rows))

    def clear(self, session_id: str) -> int:
        validate_identifier(session_id, "session_id")
        try:
            return self.chat_message.delete_many({"session_id": session_id}).deleted_count
        except Exception as exc:
            raise OperationError("HISTORY_STORE_UNAVAILABLE", "会话历史删除失败", retryable=True) from exc


_history_mongo_tool = None
_history_lock = Lock()


def get_history_mongo_tool() -> HistoryMongoTool:
    """受控懒加载；首次使用历史时才创建客户端，不创建任务或内容集合。"""
    global _history_mongo_tool
    with _history_lock:
        if _history_mongo_tool is None:
            _history_mongo_tool = HistoryMongoTool()
    return _history_mongo_tool


def save_chat_message(session_id: str, role: str, text: str, *, request_id: str, **kwargs) -> str:
    return get_history_mongo_tool().save_message(session_id, role, text, request_id=request_id, **kwargs)


def get_recent_messages(session_id: str, limit: int = 10) -> list[dict]:
    return get_history_mongo_tool().recent_messages(session_id, limit)


def clear_history(session_id: str) -> int:
    return get_history_mongo_tool().clear(session_id)


if __name__ == "__main__":
    # 仅测试本工具合同，注入 Collection 替身；不创建 MongoClient 或真实索引。
    from types import SimpleNamespace

    class CursorDouble:
        def __init__(self, rows):
            self.rows = deepcopy(rows)

        def sort(self, fields):
            for key, direction in reversed(fields):
                self.rows.sort(key=lambda item: item[key], reverse=direction == -1)
            return self

        def limit(self, count):
            self.rows = self.rows[:count]
            return self

        def __iter__(self):
            return iter(self.rows)

    class CollectionDouble:
        def __init__(self):
            self.rows = {}
            self.index_calls = 0

        def update_one(self, query, update, *, upsert):
            assert upsert is True and set(query) == {"_id", "session_id"}
            new = query["_id"] not in self.rows
            row = self.rows.setdefault(query["_id"], {"_id": query["_id"]})
            if new:
                row.update(deepcopy(update["$setOnInsert"]))
            row.update(deepcopy(update["$set"]))

        def find(self, query):
            assert set(query) == {"session_id"}
            return CursorDouble([row for row in self.rows.values() if row["session_id"] == query["session_id"]])

        def delete_many(self, query):
            keys = [key for key, row in self.rows.items() if row["session_id"] == query["session_id"]]
            for key in keys:
                del self.rows[key]
            return SimpleNamespace(deleted_count=len(keys))

        def create_index(self, fields):
            assert fields[0] == ("session_id", 1)
            self.index_calls += 1

    collection = CollectionDouble()
    history = HistoryMongoTool(collection=collection)
    assert collection.index_calls == 0
    first = history.save_message("session-a", "user", "问题", request_id="request-1")
    created = collection.rows[first]["ts"]
    history.save_message("session-a", "assistant", "回答", request_id="request-1", references=[{"reference_id": "1"}])
    history.save_message("session-b", "user", "另一个会话", request_id="request-1")
    assert history.save_message("session-a", "user", "修订问题", request_id="request-1") == first
    assert len(collection.rows) == 3 and collection.rows[first]["ts"] == created
    rows = history.recent_messages("session-a", 2)
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["text"] == "修订问题" and len(history.recent_messages("session-b")) == 1
    assert history.recent_messages("session-a", 1)[0]["role"] == "assistant"
    assert history.recent_messages("new-session") == []
    history.ensure_indexes()
    assert collection.index_calls == 1
    assert history.clear("session-a") == 2 and len(history.recent_messages("session-b")) == 1

    class BrokenCollection(CollectionDouble):
        def find(self, query):
            raise RuntimeError("private-driver-message")

    try:
        HistoryMongoTool(collection=BrokenCollection()).recent_messages("session-a")
    except OperationError as exc:
        assert exc.code == "HISTORY_STORE_UNAVAILABLE" and "private-driver" not in str(exc)
    else:
        raise AssertionError("历史读取失败不能伪装成无历史")
    print("会话历史离线检查通过：session 隔离、请求幂等、正序/最近条数、显式索引与错误语义；未连接 MongoDB。")
