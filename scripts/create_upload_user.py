"""项目根目录运行：uv run python -m scripts.create_upload_user。"""
from getpass import getpass
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from utils.clients.upload_auth import UploadAuth, password_fields


def main():
    username = input("上传管理员用户名：").strip()
    if not username or len(username) > 64:
        raise SystemExit("用户名须为 1～64 个字符")
    password = getpass("密码（至少 8 位，输入不回显）：")
    if not 8 <= len(password) <= 256:
        raise SystemExit("密码须为 8～256 个字符")
    if password != getpass("再次输入密码："):
        raise SystemExit("两次密码不一致，未创建账号")
    auth = UploadAuth()
    try:
        auth.database().upload_users.insert_one({"username": username, **password_fields(password),
            "created_at": datetime.now(timezone.utc), "disabled": False})
        print("上传账号已创建，可在 /login 登录。")
    except DuplicateKeyError:
        raise SystemExit("用户名已存在，未覆盖原账号") from None
    finally:
        auth.close()


if __name__ == "__main__":
    main()
