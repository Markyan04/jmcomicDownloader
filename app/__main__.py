import fcntl
import logging
import signal
import sys

from waitress import create_server

from . import create_app
from .config import load_settings
from .downloader import check_storage, filesystem_type
from .worker import Worker


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = load_settings()
        settings.data.mkdir(parents=True, exist_ok=True)
        if filesystem_type(settings.data) in ("nfs", "nfs4", "cifs"):
            raise ValueError("SQLite 数据目录必须位于本机磁盘，不能使用 NFS/SMB")
        lock = (settings.data / "app.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("同一数据目录已有应用运行，请勿启动多个实例")
        app = create_app(settings)
        check_storage(settings)
        server = create_server(app, host=settings.host, port=settings.port, threads=4,
                               clear_untrusted_proxy_headers=True, expose_tracebacks=False)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        logging.error("启动失败：%s", error)
        return 1

    worker = Worker(app.extensions["store"], settings)
    app.extensions["worker"] = worker

    def shutdown(signum, frame):
        logging.info("正在停止服务，等待当前下载中断")
        worker.request_stop()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker.start()
    logging.info("服务已启动 · mode=%s · http://%s:%s", settings.mode, settings.host, settings.port)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        server.close()
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
