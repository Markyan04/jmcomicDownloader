import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PASSWORDS = {"CHANGE-ME-SET-YOUR-OWN-PASSWORD"}


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    mode: str
    downloads: Path
    data: Path
    require_nfs: bool = False
    proxy: str = ""
    secure_cookie: bool = False
    host: str = "127.0.0.1"
    port: int = 3011
    min_free_mb: int = 256
    export_pdf: bool = True


def env_bool(name, default):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value not in ("true", "false"):
        raise ValueError(f"{name} 只能是 true 或 false")
    return value == "true"


def load_settings() -> Settings:
    path = Path(os.environ.get("JM_CONFIG", ROOT / "config.toml"))
    if not path.is_file():
        raise RuntimeError("缺少 config.toml，请先复制 config.example.toml 并设置账号密码")
    with path.open("rb") as file:
        config = tomllib.load(file)
    auth, download, app = config["auth"], config["download"], config.get("app", {})
    mode = os.environ.get("JM_MODE", download.get("mode", "real"))
    if mode not in ("mock", "real"):
        raise ValueError("download.mode 只能是 mock 或 real")
    if not isinstance(auth.get("username"), str) or not isinstance(auth.get("password"), str):
        raise ValueError("账号和密码必须是字符串")
    if not auth["username"].strip() or not auth["password"]:
        raise ValueError("账号和密码不能为空")
    if mode == "real" and (auth["password"] in EXAMPLE_PASSWORDS or len(auth["password"]) < 12):
        raise ValueError("请在 config.toml 设置自己的密码（至少 12 个字符），不能使用示例密码")
    if len(auth["password"]) > 512 or len(auth["username"]) > 100:
        raise ValueError("账号最多 100 个字符，密码最多 512 个字符")
    min_free_mb = download.get("min_free_mb", 256)
    if not isinstance(min_free_mb, int) or min_free_mb < 0:
        raise ValueError("download.min_free_mb 必须是非负整数")
    export_pdf = download.get("export_pdf", True)
    if not isinstance(export_pdf, bool):
        raise ValueError("download.export_pdf 必须是 true 或 false")
    return Settings(
        username=auth["username"], password=auth["password"], mode=mode,
        downloads=(ROOT / os.environ.get("JM_DOWNLOADS", download.get("directory", "downloads"))).resolve(),
        data=(ROOT / os.environ.get("JM_DATA", app.get("data_directory", "data"))).resolve(),
        require_nfs=env_bool("JM_REQUIRE_NFS", download.get("require_nfs", False)), proxy=download.get("proxy", ""),
        secure_cookie=env_bool("JM_SECURE_COOKIE", auth.get("secure_cookie", False)),
        host=os.environ.get("JM_HOST", app.get("host", "127.0.0.1")),
        port=int(os.environ.get("JM_PORT", app.get("port", 3011))),
        min_free_mb=min_free_mb,
        export_pdf=export_pdf,
    )
