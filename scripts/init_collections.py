"""首次建表：创建四个教育集合；已存在时只校验字段，不覆盖、不删除。

用法（项目根目录）：
    .\\.venv\\Scripts\\python.exe -m scripts.init_collections

API 首次建任务时也会自动调用同一逻辑（utils.clients.milvus_collections.ensure_collections），
本脚本用于单独检查/预先建表。字段不一致时以非零码退出，需要人工迁移。
"""

import sys

from common.errors import OperationError
from common.logging.logger import init_logger
from utils.clients.milvus_collections import SPECS, ensure_collections
from utils.clients.milvus_utils import get_milvus_client


def main() -> int:
    init_logger()
    client = get_milvus_client()
    if client is None:
        print("Milvus 连接失败：请检查 MILVUS_URL 与服务状态")
        return 2

    existed = {name: client.has_collection(collection_name=name) for name, _, _ in SPECS}
    try:
        created = ensure_collections(client)
    except OperationError as exc:
        print(f"[失败] {exc.code}: {exc.message}")
        return 1

    for name, _, _ in SPECS:
        fields = client.describe_collection(collection_name=name).get("fields", [])
        state = "已创建" if name in created else ("已存在" if existed[name] else "已存在")
        print(f"[{state}] {name}：{len(fields)} 个字段")
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

