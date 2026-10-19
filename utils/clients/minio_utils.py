
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


if __name__ == "__main__":
    from unittest.mock import Mock, call, patch

    from common.config.minio_config import MinIOConfig
    from common.logging.logger import init_logger

    init_logger()
    logger.info("MinIO 工具离线自测开始：3 个案例，不连接真实服务")
    config = MinIOConfig(
        endpoint="localhost:9000", access_key="offline-access",
        secret_key="offline-secret", bucket_name="offline-bucket",
    )
    cases = (("桶已存在", True, False), ("桶不存在，自动创建", False, False), ("初始化失败", False, True))
    for index, (description, exists, fail) in enumerate(cases, 1):
        logger.info("案例 {}/3：{}", index, description)
        client = Mock(spec=Minio)
        client.bucket_exists.return_value = exists
        failure = RuntimeError("离线替身模拟建桶失败")
        if fail:
            client.make_bucket.side_effect = failure

        # 替换 SDK 构造函数，仍经过真实工厂、配置校验、桶准备与缓存流程。
        with patch(f"{__name__}.Minio", return_value=client) as constructor, \
                patch(f"{__name__}.minio_config", config), \
                patch(f"{__name__}._minio_client", None):
            if fail:
                try:
                    get_minio_client()
                except RuntimeError as exc:
                    assert exc is failure and _minio_client is None
                else:
                    raise AssertionError("初始化失败必须向上传递")
                logger.info("初始化失败已按预期抛出，缓存为空；随后重试")
                client.make_bucket.side_effect = None

            assert get_minio_client() is client
            assert get_minio_client() is client  # 缓存命中，不再次创建客户端或检查桶。
            attempts = 2 if fail else 1
            assert constructor.call_args_list == [call(
                config.endpoint, access_key=config.access_key,
                secret_key=config.secret_key, secure=config.minio_secure,
            )] * attempts
            expected_calls = [call.bucket_exists(config.bucket_name)]
            if not exists:
                expected_calls.append(call.make_bucket(config.bucket_name))
            assert client.method_calls == expected_calls * attempts
            client.set_bucket_policy.assert_not_called()
            client.delete_bucket_policy.assert_not_called()
        logger.success("案例通过：{}", description)
    logger.success("MinIO 工具离线自测结束：3/3 通过；未连接真实 MinIO 或创建真实桶")
