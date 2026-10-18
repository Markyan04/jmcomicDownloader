# JMComic Downloader

轻量的 JM 下载管理工具，支持固定账号登录、任务队列、下载进度、历史记录、失败重试、整本 PDF 合并和在线阅读。基于 Flask、SQLite 和 [jmcomic](https://github.com/hect0x7/JMComic-Crawler-Python)，单容器运行，无需额外数据库或前端构建。

## Docker Compose 部署

要求 Docker Engine / Docker Desktop 已启动，并安装 Docker Compose 插件。

```sh
python3 scripts/setup.py
# 查看或修改 config.toml 中的账号密码
docker compose up -d --build --wait --wait-timeout 90
```

访问 <http://127.0.0.1:3011>。

初始化脚本生成随机密码和当前用户 UID/GID，重复运行不会覆盖已有配置。也可以手动复制 `config.example.toml` 为 `config.toml`、复制 `.env.example` 为 `.env`，设置自己的账号密码，再创建 `data/` 和 `downloads/`。

默认保存到项目的 `downloads/`。使用外部存储时，在 `.env` 中配置已存在且可写的绝对路径：

```dotenv
DOWNLOAD_DIR=/path/to/downloads
```

`data/` 保存任务数据库和会话密钥，必须位于本机磁盘。配置、数据库、图片和 PDF 均不写入镜像。容器按 `.env` 中的 UID/GID 运行，使用只读根文件系统与轮转日志。

默认只监听回环地址；对外提供服务时使用 HTTPS 反向代理，并启用安全 Cookie。书库、阅读器和 PDF 数据接口都需要登录。

## 从第一版升级

在原部署目录更新源码后，直接重新构建并替换容器：

```sh
git pull --ff-only
docker compose up -d --build --wait --wait-timeout 90
```

使用源码压缩包部署的用户，解压新版后只覆盖应用源码、Dockerfile 和依赖文件，保留原来的 `config.toml`、`.env`、`compose.yaml`、`data/` 和下载目录，然后执行同一条 Compose 命令。容器内没有新增系统依赖或服务，已有 Compose 文件可以继续使用。升级前可按下文维护说明备份；无需初始化、清空数据库、迁移下载目录或重新下载资源。

新版在顶部增加“本地书库”入口，直接读取原来挂载的下载目录。任务数据库结构和下载状态机保持不变；升级重启时，正在执行的任务仍按原规则标为失败，需手动重试。建议等当前下载完成后升级。

## 本地书库与在线阅读

书库以 `download.directory`（Docker 内为 `/downloads`）为根目录，按当前目录分页显示子目录和 `.pdf` 文件，点击文件进入阅读器。第一版生成的旧目录、任意层级的现有 PDF、手动复制到下载目录的 PDF 都可直接读取，**不依赖下载任务记录**。添加文件后刷新书库即可；不会建立另一套索引或修改文件。只有图片、尚无 PDF 的目录暂不提供在线阅读，也不会自动转换历史图片。

阅读器使用随应用打包的 Mozilla PDF.js，支持上一页 / 下一页、页码跳转、左右方向键和缩放。通过 HTTP Range 分段读取，当前页准备好即可显示，随后持续在后台加载剩余内容，**停留在同一页也会继续缓存，直到整本加载完成**。阅读器显示整体缓存进度，已加载的数据在当前阅读会话中供翻页复用；离开阅读器会停止加载，重新打开会建立新的阅读会话，不提供持久离线缓存。

数据加载与页面绘制独立：后台缓存整本 PDF 的压缩数据，只渲染当前页，避免把整本漫画解码为图片驻留内存；浏览器仍需要容纳 PDF 文件数据的内存。首次显示需要 PDF 的结构信息和当前页数据，具体读取量取决于 PDF；很小的 PDF 或结构特殊的文件可能需要读取全部数据。这里的“边加载边看”指已生成 PDF 的网络加载过程，不包含还在生成的半成品。

浏览仅开放根目录内的普通目录和 PDF；隐藏文件、符号链接及目录穿越均拒绝访问，不支持任意外部 URL。PDF 接口保留登录校验、禁止公共缓存，并支持 `HEAD`、`206 Partial Content`、`416` 和文件版本校验。阅读期间文件被替换时会提示刷新。

反向代理需要转发 `Range` / `If-Range`，保留 `Content-Length`、`Content-Range`、`Accept-Ranges`、`ETag`，不要对 `/api/library/file` 强制压缩、整文件缓冲或公共缓存。例如 Nginx 在现有代理配置中可添加（`app_backend` 替换为自己的上游名称）：

```nginx
location = /api/library/file {
    proxy_pass http://app_backend;
    proxy_set_header Range $http_range;
    proxy_set_header If-Range $http_if_range;
    proxy_buffering off;
    proxy_cache off;
    gzip off;
}
```

无需将 PDF 线性化或重新生成，正常的已有 PDF 即可按范围读取。PDF.js 的脚本、Worker、字体及解码资源均由容器提供，阅读时不依赖外部 CDN。

## 应用配置

在 `config.toml` 中配置：

```toml
[auth]
username = "admin"
password = "CHANGE-ME-SET-YOUR-OWN-PASSWORD"
secure_cookie = false

[download]
mode = "real"
directory = "downloads"
require_nfs = false
proxy = ""
min_free_mb = 256
export_pdf = true

[app]
data_directory = "data"
host = "127.0.0.1"
port = 3011
```

| 配置 | 说明 |
| --- | --- |
| `auth.username` / `auth.password` | 固定账号密码；真实模式拒绝示例密码，密码至少 12 个字符 |
| `auth.secure_cookie` | HTTPS 部署设为 `true`；本机 HTTP 设为 `false` |
| `download.mode` | `real` 为真实下载；`mock` 仅供测试 |
| `download.require_nfs` | 需要 NFS 时设为 `true`，挂载错误会拒绝启动和下载 |
| `download.proxy` | 可选代理 URL；空字符串表示直连 |
| `download.min_free_mb` | 开始任务前要求的最小剩余空间 |
| `download.export_pdf` | 默认 `true`，整本图片下载完成后生成一份 PDF |

Docker 内固定使用 `/app/data` 和 `/downloads`，宿主机输出位置由 Compose 的挂载配置决定。代理必须从容器中可达；`host.docker.internal` 可用于访问宿主机。配置文件修改后执行 `docker compose restart`。

`.env` 可配置：

| 变量 | 默认值 / 用途 |
| --- | --- |
| `APP_UID` / `APP_GID` | `1000` / `1000`；初始化脚本会使用当前用户的值 |
| `DOWNLOAD_DIR` | `./downloads` |
| `BIND_ADDRESS` / `PORT` | `127.0.0.1` / `3011` |
| `JM_MODE` | `real`；仅测试时改为 `mock` |
| `JM_SECURE_COOKIE` | 留空使用应用配置，或明确设置 `true` / `false` |
| `PYTHON_IMAGE` | `python:3.12-slim`；可选择其他可信的 Python 3.12 基础镜像 |

修改 `.env` 或 Compose 后执行 `docker compose up -d --build --force-recreate`。

## 下载与 PDF

输入正整数编号或 `JM` 前缀编号即可提交。默认一次下载一本、一个章节并发、四张图片并发。

```text
排队中 → 下载中 → 已完成
             ↘ 失败 → 手动重试 → 排队中
```

同一活动编号会去重，手动重试放到队尾。关闭页面不影响下载；容器停止或异常退出后，未完成任务会标为失败，等待手动重试。

默认输出结构：

```text
downloads/
└── [示例书名]+[示例作者]+JM123/
    ├── JM123.pdf
    └── 章节编号/
        ├── 00001.webp
        └── 00002.webp
```

目录格式为 `[书名]+[作者]+JM编号`，多个作者用 `、` 连接，未知作者使用“未知作者”。名称中的非法路径字符会替换，过长名称会截短，并始终保留 JM 编号。PDF 文件名仍为 `JM编号.pdf`。

PDF 使用下载库提供的有序清单，按章节和图片顺序整合，每张图片对应一页，保持宽高比，不拉伸或裁切。WebP 等格式在嵌入 PDF 时编码为高质量 JPEG（质量 95），原始图片保留。逐页处理，不同时加载整本解码图片。

任务在转换期间仍显示“下载中”，并显示 PDF 合并进度；只有完整 PDF 写入成功后才显示“已完成”。失败或中断不会发布半成品 PDF，重试可以复用校验通过的原图。设置 `export_pdf = false` 可只保存图片。

## 运行与维护

```sh
docker compose ps
docker compose logs -f --tail=100
docker compose restart
docker compose down
```

健康检查覆盖服务、下载线程和任务数据库。`down` 不删除宿主机数据。备份时先停止应用，备份 `data/`、`config.toml` 和 `.env`，再启动；下载目录单独备份。

只支持单进程、单实例；文件锁防止多个实例同时使用同一数据库。使用网络存储时，应先挂载再创建容器；重新挂载后需重建容器。网络文件系统的阻塞 I/O 可能无法即时中止。

## 开发与测试

需要 Python 3.11+：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
python3 scripts/setup.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m app
```

测试使用临时目录和合成图片，不下载真实内容。模拟模式可通过 `JM_MODE=mock` 启用；编号 `999999` 首次模拟失败，重试后成功。

运行依赖固定在 `requirements.lock`，测试依赖在 `requirements-dev.txt`。账号配置、环境变量文件、数据库和下载资源均由 `.gitignore` 排除，请勿提交真实凭据或私人部署配置。

## 与上游的关系

构建时通过 pip 从 PyPI 安装固定版本的 `jmcomic`，运行时使用 `import jmcomic` 和 `jmcomic.download_album(...)` 调用 Python API。本项目通过自定义下载器收集进度，并用 Pillow / ReportLab 生成 PDF；不克隆或内嵌上游源码，也不将其编译为独立二进制程序。

## License

本项目使用 [MIT License](LICENSE)。`jmcomic` 的许可证和维护归其上游项目所有。在线阅读使用 Mozilla PDF.js（Apache-2.0），随附资源的许可证保留在 [vendor/pdfjs](app/static/vendor/pdfjs/) 及其子目录中。
