
import json
# 导入MinIO官方Python SDK核心类（用于MinIO对象存储的客户端操作）
from minio import Minio

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
        secure=minio_config.minio_secure  # https True  http False
    )
    return client

def _create_minio_bucket(client:Minio):
    # 判断是否存在
    if not client.bucket_exists(minio_config.bucket_name):
        # 不存在
        client.make_bucket(minio_config.bucket_name)
        # 不存在创建桶,并且设置访问权限
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": "*"},
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{minio_config.bucket_name}/*",
                },
            ],
        }
        client.set_bucket_policy(minio_config.bucket_name, json.dumps(policy))
    else:
        logger.info(f"{minio_config.bucket_name}已经存在,无需再次创建!")

def get_minio_client():
    """校验配置并创建、缓存客户端；不检查/创建桶，也不修改访问策略。"""
    global _minio_client
    if not _minio_client:
        client = _create_minio_client()
        _minio_client = client
    return _minio_client
