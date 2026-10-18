import logging
import errno
import threading

from .downloader import DownloadError, check_storage, mock_download, real_download

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run, name="download-worker", daemon=True)
        self.current_task = None

    def start(self):
        self.store.recover()
        self.thread.start()

    def request_stop(self):
        self.stop_event.set()
        self.wake.set()

    def stop(self):
        self.request_stop()
        self.thread.join(timeout=35)
        if self.current_task:
            self.store.interrupt(self.current_task)
        if self.thread.is_alive():
            log.warning("Download still blocked on I/O; task marked interrupted")

    def run_one(self):
        task = self.store.claim(self.settings.mode)
        if not task:
            return False
        self.current_task = task["id"]
        log.info("Task %s started (JM%s)", task["id"], task["jm_id"])
        try:
            check_storage(self.settings)
            download = mock_download if self.settings.mode == "mock" else real_download
            output = download(task, self.settings,
                              lambda **fields: self.store.update(task["id"], **fields), self.stop_event)
            if self.stop_event.is_set():
                raise DownloadError("应用停止，下载已中断，请手动重试")
            message = "下载完成，PDF 已生成" if output.endswith(".pdf") else "下载完成"
            self.store.update(task["id"], status="completed", message=message, output=output)
            log.info("Task %s completed", task["id"])
        except Exception as error:
            # Do not persist upstream exception text: it may contain proxy credentials or URLs.
            if self.stop_event.is_set():
                message = "应用停止，下载已中断，请手动重试"
            elif isinstance(error, (DownloadError, PermissionError, FileNotFoundError)):
                message = str(error) if isinstance(error, DownloadError) else "下载目录不存在或无写入权限"
            elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
                message = "下载存储已满，请清理空间后重试"
            else:
                message = {
                    "MissingAlbumPhotoException": "没有找到这个编号的内容，请检查 JM 号",
                    "RequestRetryAllFailException": "上游请求多次失败，请检查下载代理和网络后重试",
                    "PartialDownloadFailedException": "部分图片下载失败，已有图片已保留，请手动重试",
                }.get(type(error).__name__, f"下载失败（{type(error).__name__}），请检查网络、编号或下载配置后重试")
            self.store.update(task["id"], status="failed", message=message[:400])
            log.warning("Task %s failed: %s", task["id"], type(error).__name__)
        finally:
            self.current_task = None
        return True

    def run(self):
        while not self.stop_event.is_set():
            self.wake.clear()
            try:
                if self.run_one():
                    continue
            except Exception:
                log.exception("Queue access failed")
            self.wake.wait(timeout=2)
