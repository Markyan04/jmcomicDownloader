"""Prepare local files without replacing existing credentials. Python 3.9+."""
import argparse
import os
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def prepare(root):
    config = root / "config.toml"
    sample = root / "config.example.toml"
    created = not config.exists()
    content = sample.read_text(encoding="utf-8") if created else config.read_text(encoding="utf-8")
    pattern = r'(?m)^password\s*=\s*"CHANGE-ME-SET-YOUR-OWN-PASSWORD"[^\n]*$'
    if created:
        password = secrets.token_urlsafe(24)
        content = re.sub(pattern, f'password = "{password}"', content)
        content = re.sub(r'(?m)^mode\s*=.*$', 'mode = "real"', content)
        config.write_text(content, encoding="utf-8")
        config.chmod(0o600)
        print("已生成真实下载配置与随机密码；账号密码请在 config.toml 中查看或修改。")
    else:
        print("保留已有 config.toml，未修改账号密码。")
    (root / "data").mkdir(exist_ok=True)
    (root / "downloads").mkdir(exist_ok=True)
    env = root / ".env"
    if not env.exists():
        env.write_text(f"APP_UID={os.getuid()}\nAPP_GID={os.getgid()}\n", encoding="utf-8")
        env.chmod(0o600)
        print("已写入 .env，容器将使用当前用户的 UID/GID。")
    print("就绪。使用 docker compose up -d --build 启动。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    prepare(ROOT)
