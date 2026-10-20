
# 导入MinIO官方Python SDK核心类（用于MinIO对象存储的客户端操作）
from minio import Minio
from urllib3 import PoolManager, Timeout
from urllib3.util.retry import Retry

# 导入项目内部配置与日志工具
from common.config.minio_config import minio_config  # MinIO相关配置（端点、密钥、桶名等）
from common.logging.logger import logger            # 项目统一日志工具

# 全局MinIO客户端实例（单例模式，避免重复创建连接，提升性能）
_minio_client = None

# 使用现有配置创建客户端，不执行桶管理。
def _create_minio_client():
    minio_config.validate()
    client = Minio(
        minio_config.endpoint,
        access_key=minio_config.access_key,
        secret_key=minio_config.secret_key,
        secure=minio_config.minio_secure,  # https True  http False
        # 网络不可用时有界返回，避免 Ctrl+C 收尾被 SDK 长时间重试拖住。
        http_client=PoolManager(timeout=Timeout(connect=5, read=15),
                               retries=Retry(total=1, backoff_factor=0.2)),
    )
    return client

def _create_minio_bucket(client: Minio):
    """桶存在则复用，不存在则创建；不设置或删除任何桶策略。"""
    # 新桶保留默认私有权限；已有桶的权限保持原状。
    if not client.bucket_exists(minio_config.bucket_name):
        client.make_bucket(minio_config.bucket_name)
        logger.success("MinIO 桶 {} 创建成功，保留默认私有权限", minio_config.bucket_name)
    else:
        logger.info("MinIO 桶 {} 已存在，直接复用", minio_config.bucket_name)

def get_minio_client():
    """首次初始化时准备桶，全部成功后缓存；失败直接向上传递。"""
    global _minio_client
    # 1. 已有缓存客户端时直接复用，不重复检查桶。
    if _minio_client is not None:
        return _minio_client

    # 2. 复用工厂校验配置并创建客户端。
    client = _create_minio_client()
    # 3. 检查桶，不存在则创建；不设置或删除桶策略。
    _create_minio_bucket(client)
    # 4. 全部初始化成功后才缓存，前面任一步失败都不会缓存该实例。
    _minio_client = client
    return _minio_client
