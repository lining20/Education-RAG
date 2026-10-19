"""共享 MongoDB 客户端创建入口；复用集中配置，生命周期由 Service 管理。"""

from common.config.mongo_config import MongoConfig, mongo_config


def create_mongo_client(config: MongoConfig = mongo_config):
    """显式调用才构造客户端；不建索引/集合，不导入旧历史模块，不主动 ping。"""
    config.validate()
    from pymongo import MongoClient

    return MongoClient(
        config.mongo_url, connect=False,
        connectTimeoutMS=config.connect_timeout_ms,
        serverSelectionTimeoutMS=config.server_selection_timeout_ms,
        socketTimeoutMS=config.socket_timeout_ms,
    )
