"""按文件名列举 MinIO 对象；--task 仅用于排查历史任务目录。
当前布局：imports/{文件名}/。只读，不删除对象。"""

import argparse
from datetime import timedelta
import sys

from common.config.minio_config import minio_config
from utils.clients.minio_utils import get_minio_client


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="列举文档在 MinIO 中的对象")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="原文件名，例如 课程介绍.md")
    group.add_argument("--task", help="历史任务标识（旧布局）")
    parser.add_argument("--kind", choices=("all", "resources", "payloads", "source"), default="all",
                        help="只看某一类对象")
    parser.add_argument("--urls", action="store_true", help="为每个对象生成临时链接")
    parser.add_argument("--minutes", type=int, default=60, help="临时链接有效期（分钟）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    client = get_minio_client()
    bucket = minio_config.bucket_name
    if not bucket:
        print("缺少 MINIO_BUCKET_NAME 配置")
        return 2

    if args.file:
        prefix = f"imports/{args.file.strip()}/"
    else:
        prefix = f"imports/{args.task.strip()}/"
    try:
        objects = sorted(client.list_objects(bucket, prefix=prefix, recursive=True),
                         key=lambda item: item.object_name)
    except Exception as exc:
        print(f"列举失败：{type(exc).__name__} {str(exc)[:160]}")
        return 1
    if not objects:
        print(f"没有对象：bucket={bucket} prefix={prefix}")
        return 1

    selected = []
    for item in objects:
        name = item.object_name
        if args.kind == "resources" and "/resources/" not in name:
            continue
        if args.kind == "payloads" and "/payloads/" not in name:
            continue
        if args.kind == "source" and ("/resources/" in name or "/payloads/" in name):
            continue
        selected.append(item)

    total = 0
    for item in selected:
        size = getattr(item, "size", 0) or 0
        total += size
        line = f"{item.object_name}  {size / 1024:.1f} KB"
        if args.urls:
            try:
                line += "\n    " + client.presigned_get_object(
                    bucket, item.object_name, expires=timedelta(minutes=args.minutes))
            except Exception as exc:
                line += f"\n    <链接生成失败：{type(exc).__name__}>"
        print(line)

    print(f"\n共 {len(selected)} 个对象，合计 {total / 1024 / 1024:.2f} MB；bucket={bucket}")
    print("图片位于 …/resources/，对象名由 resource_id 与内容指纹组成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

