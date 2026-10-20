"""上传权限：MongoDB 保存账号和登录会话，密码与会话令牌均不明文落库。"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from threading import Lock
from time import monotonic

from pymongo.errors import PyMongoError

from common.config.mongo_config import mongo_config
from common.errors import OperationError
from utils.clients.mongo_utils import create_mongo_client

COOKIE_NAME = "education_upload_session"
SESSION_SECONDS = 8 * 60 * 60
PASSWORD_ITERATIONS = 600_000


def password_fields(password):
    """独立随机盐 + PBKDF2；不存密码，也不把密码输出到日志。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ITERATIONS)
    return {"password_salt": salt, "password_hash": digest.hex(), "password_iterations": PASSWORD_ITERATIONS}


class UploadAuth:
    def __init__(self):
        self.client = None
        self._lock = Lock()
        self._attempts = {}

    def database(self):
        # 仅进入登录/上传功能时连接，不影响公开检索首页启动。
        with self._lock:
            if self.client is None:
                client = create_mongo_client()
                try:
                    database = client[mongo_config.db_name]
                    database.upload_users.create_index("username", unique=True)
                    database.upload_sessions.create_index("expires_at", expireAfterSeconds=0)
                except Exception:
                    client.close()
                    raise
                self.client = client
        return self.client[mongo_config.db_name]

    def login(self, username, password, address):
        # 单进程内限制同一来源的登录频率；不阻塞等待、不记录密码。
        now = monotonic()
        with self._lock:
            self._attempts = {key: stamps for key, stamps in self._attempts.items() if stamps[-1] > now - 60}
            attempts = [stamp for stamp in self._attempts.get(address, []) if stamp > now - 60]
            if len(attempts) >= 10:
                raise OperationError("LOGIN_RATE_LIMITED", "登录尝试过于频繁，请一分钟后重试")
            self._attempts[address] = [*attempts, now]
        try:
            database = self.database()
            user = database.upload_users.find_one({"username": username})
            # 无此用户也执行相同的密码计算，统一返回错误信息。
            salt = user["password_salt"] if user else "00" * 16
            iterations = user["password_iterations"] if user else PASSWORD_ITERATIONS
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
            expected = user["password_hash"] if user else "00" * 32
            if not hmac.compare_digest(digest, expected) or not user or user.get("disabled"):
                raise OperationError("INVALID_CREDENTIALS", "用户名或密码不正确")
            token = secrets.token_urlsafe(32)
            database.upload_sessions.insert_one({"_id": hashlib.sha256(token.encode()).hexdigest(),
                "user_id": user["_id"], "expires_at": datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)})
            return token, user["username"]
        except PyMongoError as exc:
            raise OperationError("AUTH_STORE_UNAVAILABLE", "无法访问账号数据库，请检查 MongoDB 服务", retryable=True) from exc

    def current_user(self, token):
        if not token or len(token) > 128:
            return None
        try:
            database = self.database()
            session = database.upload_sessions.find_one({"_id": hashlib.sha256(token.encode()).hexdigest(),
                                                        "expires_at": {"$gt": datetime.now(timezone.utc)}})
            if not session:
                return None
            user = database.upload_users.find_one({"_id": session["user_id"], "disabled": {"$ne": True}}, {"username": 1})
            return {"username": user["username"]} if user else None
        except PyMongoError as exc:
            raise OperationError("AUTH_STORE_UNAVAILABLE", "无法验证上传权限，请检查 MongoDB 服务", retryable=True) from exc

    def logout(self, token):
        if token:
            try:
                self.database().upload_sessions.delete_one({"_id": hashlib.sha256(token.encode()).hexdigest()})
            except PyMongoError as exc:
                raise OperationError("AUTH_STORE_UNAVAILABLE", "退出登录失败，请检查 MongoDB 服务后重试", retryable=True) from exc

    def close(self):
        if self.client is not None:
            self.client.close()
