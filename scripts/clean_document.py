"""按文件名清理知识库中的历史版本（一次性迁移工具）。

场景：手动清理指定文件名的向量；正常同名重传已由导入流程自动替换。

用法：
    .\\.venv\\Scripts\\python.exe -m scripts.clean_document --name "课程介绍.md" --dry-run
    .\\.venv\\Scripts\\python.exe -m scripts.clean_document --name "课程介绍.md" --yes

只删除 Milvus 中 source_file_name 等于给定名称的行，不删除 MinIO 原始文件、
不删除其他文件、不改动集合结构。--dry-run 只统计数量。
"""

import argparse
import sys

from common.config.milvus_config import milvus_config
from common.logging.logger import init_logger
from utils.clients.milvus_utils import get_milvus_client
from utils.clients.milvus_utils import escape_milvus_string

COLLECTIONS = (
    milvus_config.document_index_collection,
    milvus_config.courses_collection,
    milvus_config.document_chunks_collection,
    milvus_config.questions_collection,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按 source_file_name 清理知识库历史版本")
    parser.add_argument("--name", required=True, help="原始文件名，例如 课程介绍.md")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    parser.add_argument("--yes", action="store_true", help="确认执行删除")
    args = parser.parse_args(argv)
    if not args.dry_run and not args.yes:
        parser.error("删除操作需要显式加 --yes；建议先跑 --dry-run")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    init_logger()
    client = get_milvus_client()
    if client is None:
        print("Milvus 连接失败：请检查 MILVUS_URL 与服务状态")
        return 2

    target = escape_milvus_string(args.name)
    expr = f'source_file_name == "{target}"'
    total = 0
    for name in COLLECTIONS:
        try:
            if args.dry_run:
                rows = client.query(collection_name=name, filter=expr, output_fields=["count(*)"])
                count = rows[0]["count(*)"] if rows else 0
            else:
                response = client.delete(collection_name=name, filter=expr)
                count = response.get("delete_count", 0) if isinstance(response, dict) else 0
        except Exception as exc:
            print(f"[失败] {name}: {type(exc).__name__} {str(exc)[:160]}")
            return 1
        total += count
        action = "待删除" if args.dry_run else "已删除"
        print(f"[{action}] {name}: {count} 行")

    print(f"合计 {total} 行；{'未做修改（dry-run）' if args.dry_run else '完成'}")
    if not args.dry_run:
        print("提示：同名文件的所有记录已清理；重新导入会创建独立任务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

