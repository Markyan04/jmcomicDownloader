import logging
import errno
import threading

from .downloader import DownloadError, check_storage, mock_download, real_download
from .task_files import remove_task_files

log = logging.getLogger(__name__)


class TaskStop:
    def __init__(self, shutdown, cancel):
        self.shutdown, self.cancel = shutdown, cancel

    def is_set(self):
        return self.shutdown.is_set() or self.cancel.is_set()

    def wait(self, timeout):
        return self.shutdown.wait(timeout) or self.cancel.is_set()


class Worker:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run, name="download-worker", daemon=True)
        self.current_task = None
        self.task_lock = threading.Lock()
        self.cancel_event = None

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

    def request_delete(self, task_id):
        with self.task_lock:
            self.store.request_delete(task_id)
            if self.current_task == task_id and self.cancel_event:
                self.cancel_event.set()
                self.wake.set()
                return False
            # Queued/completed tasks need not wait behind another book's download.
            if not self.clean_task(self.store.get(task_id)):
                raise ValueError("资源清理失败，记录已保留；请检查存储和权限后再次删除")
        self.wake.set()
        return True

    def clean_task(self, task):
        try:
            remove_task_files(task, self.settings)
        except (OSError, ValueError) as error:
            self.store.delete_failed(task["id"])
            log.warning("Task %s cleanup failed: %s", task["id"], type(error).__name__)
            return False
        else:
            self.store.finish_delete(task["id"])
            log.info("Task %s and its files removed", task["id"])
            return True

    def run_one(self):
        with self.task_lock:
            task = self.store.next_delete() or self.store.claim(self.settings.mode)
            if not task:
                return False
            self.current_task = task["id"]
            self.cancel_event = threading.Event()
            stop = TaskStop(self.stop_event, self.cancel_event)
        log.info("Task %s started (JM%s)", task["id"], task["jm_id"])
        try:
            if task["delete_requested"]:
                return True  # Finally handles cleanup, including requests saved before restart.
            check_storage(self.settings)
            download = mock_download if self.settings.mode == "mock" else real_download
            output = download(task, self.settings,
                              lambda **fields: self.store.update(task["id"], **fields), stop)
            if stop.is_set():
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
            # Upstream has joined its image threads before returning. Never remove
            # files while the downloader or PDF writer can still write to them.
            try:
                current = self.store.get(task["id"])
                if current and current["delete_requested"]:
                    self.clean_task(current)
            finally:
                with self.task_lock:
                    self.current_task = None
                    self.cancel_event = None
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
