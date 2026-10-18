import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path


class DownloadError(RuntimeError):
    """A user-facing failure with no upstream credentials or response bodies."""


def book_directory_name(title, authors, jm_id):
    def clean(value, fallback, max_bytes):
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", str(value or ""))
        value = value.strip().strip(". ")
        return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore") or fallback

    title = clean(title, "未命名", 120)
    author = clean("、".join(authors or []), "未知作者", 80)
    return f"[{title}]+[{author}]+JM{jm_id}"


def filesystem_type(path: Path):
    # Inspect the closest covering mount; a bind mount alone does not prove NFS.
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return None
    matches = []
    for line in mountinfo.read_text().splitlines():
        left, right = line.split(" - ", 1)
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), left.split()[4])
        if path == Path(mount) or Path(mount) in path.parents:
            matches.append((len(mount), right.split()[0]))
    return max(matches)[1] if matches else None


def check_storage(settings):
    directory = settings.downloads
    if settings.require_nfs and filesystem_type(directory) not in ("nfs", "nfs4"):
        raise DownloadError("下载目录不是 NFS 文件系统，请检查存储挂载后重建容器")
    if not directory.is_dir():
        raise DownloadError("下载目录不存在，请先创建目录并检查挂载")
    if shutil.disk_usage(directory).free < settings.min_free_mb * 1024 * 1024:
        raise DownloadError(f"下载目录可用空间不足 {settings.min_free_mb} MB，请清理存储后重试")
    with tempfile.NamedTemporaryFile(prefix=".jm-write-check-", dir=directory) as file:
        file.write(b"storage-check")
        file.flush()
        os.fsync(file.fileno())


def mock_download(task, settings, report, stop):
    total = 24
    report(title=f"模拟任务 · JM{task['jm_id']}", total=total, message="正在模拟下载")
    for done in range(1, total + 1):
        if stop.wait(0.18):
            raise DownloadError("应用停止，下载已中断，请手动重试")
        if task["jm_id"] == "999999" and task["attempt"] == 1 and done == 9:
            raise DownloadError("模拟网络错误：点击重试后，此任务会成功")
        report(done=done)
    folder = settings.downloads / "_mock" / f"task-{task['id']}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "README.txt").write_text(f"模拟任务 JM{task['jm_id']}，没有下载真实内容。\n", encoding="utf-8")
    return str(folder)


def real_download(task, settings, report, stop):
    import jmcomic
    from PIL import Image

    class StopControl(jmcomic.DownloadControl):
        @property
        def is_cancelled(self):
            return stop.is_set() or super().is_cancelled

        @property
        def reason(self):
            return "应用停止，下载已中断，请手动重试" if stop.is_set() else super().reason

    def valid_image(path):
        try:
            with Image.open(path) as image:
                image.verify()
            return True
        except (OSError, SyntaxError, ValueError):
            return False

    option = jmcomic.JmOption.construct({
        "log": False,
        "client": {"impl": "api", "retry_times": 3, "cache": True,
                   "postman": {"meta_data": {"proxies": settings.proxy or None, "timeout": 30}}},
        "download": {"threading": {"photo": 1, "image": 4}, "cache": True},
        "dir_rule": {"base_dir": str(settings.downloads), "rule": "Bd / {Pid}"},
    })
    # Resolve actual chapter lengths up front to keep the denominator stable.
    client = option.build_jm_client()
    album = client.get_album_detail(task["jm_id"])
    folder = settings.downloads / book_directory_name(album.title, album.authors, task["jm_id"])
    option.dir_rule = jmcomic.DirRule("Bd / {Pid}", base_dir=str(folder))
    total = 0
    for chapter in album:
        if stop.is_set():
            raise DownloadError("应用停止，下载已中断，请手动重试")
        photo = client.get_photo_detail(chapter.id)
        client.check_photo(photo)
        total += len(photo)
    if total <= 0:
        raise DownloadError("没有找到可下载的图片")
    report(title=album.title, total=total, message="正在下载图片")
    lock = threading.Lock()
    saved = set()
    last_report = 0.0

    class ProgressDownloader(jmcomic.JmDownloader):
        def before_image(self, image, img_save_path):
            if stop.is_set():
                raise DownloadError("应用停止，下载已中断，请手动重试")
            super().before_image(image, img_save_path)
            if image.exists and image.cache and not valid_image(img_save_path):
                image.cache = False # Retry must replace a corrupt cache file.

        def after_image(self, image, img_save_path):
            nonlocal last_report
            path = Path(img_save_path)
            if not path.is_file() or not valid_image(path):
                raise DownloadError("图片保存结果无效，请重试")
            super().after_image(image, img_save_path)
            with lock:
                saved.add(str(path))
                current = time.monotonic()
                if current - last_report >= 0.3 or len(saved) == total:
                    report(done=len(saved))
                    last_report = current

    result = jmcomic.download_album(task["jm_id"], option, downloader=ProgressDownloader, control=StopControl())
    downloader = result[1]
    if downloader.has_download_failures or len(saved) != total:
        raise DownloadError(f"下载不完整：已保存 {len(saved)} / {total} 张，请重试")
    report(done=total)
    if not settings.export_pdf:
        return str(folder)
    from .pdf_export import PDFExportError, write_pdf
    paths = result.manifest.image_filepath_list
    if len(paths) != total:
        raise DownloadError("图片清单不完整，无法生成 PDF，请重试")
    report(message=f"正在生成 PDF（0 / {total} 页）")
    last_pdf_report = 0.0

    def pdf_progress(done, count):
        nonlocal last_pdf_report
        current = time.monotonic()
        if current - last_pdf_report >= 0.3 or done == count:
            report(message=f"正在生成 PDF（{done} / {count} 页）")
            last_pdf_report = current

    try:
        output = write_pdf(paths, folder / f"JM{task['jm_id']}.pdf", stop, pdf_progress)
    except PDFExportError as error:
        raise DownloadError(f"PDF 生成失败：{error}") from error
    return str(output)
