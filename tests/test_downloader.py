import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace

import jmcomic
from PIL import Image
from pypdf import PdfReader

from app.config import Settings
from app.downloader import book_directory_name, real_download


class DownloaderTests(unittest.TestCase):
    """Exercise the installed upstream downloader, substituting only network I/O."""

    def test_book_directory_handles_unsafe_and_long_metadata(self):
        name = book_directory_name('../测试/标题\\:*?"<>|', ['作者/甲', '作者乙'], '123')
        self.assertEqual(Path(name).name, name)
        self.assertNotIn('\\', name)
        self.assertNotIn('..', name)
        self.assertTrue(name.endswith(']+JM123'))
        long_name = book_directory_name('书名' * 200, ['作者' * 200], '123456789012')
        self.assertLessEqual(len(long_name.encode('utf-8')), 240)
        self.assertEqual(book_directory_name('', [], '123'), '[未命名]+[未知作者]+JM123')

    def test_multiple_chapters_use_reading_order_in_one_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings("admin", "password", "real", Path(temp), Path(temp) / "data")
            album = jmcomic.JmAlbumDetail("123", "1", "Book", [("900", "1", "First"), ("100", "2", "Second")],
                                         4, "", "", "0", "0", 0, [], [], [], [])
            photos = {pid: jmcomic.JmPhotoDetail(pid, "Chapter", "123", index, scramble_id="1",
                                                page_arr=["00002.webp", "00010.webp"],
                                                data_original_domain="example.invalid", from_album=album)
                      for index, pid in enumerate(("900", "100"), 1)}

            class Client:
                def get_album_detail(self, _):
                    return album

                def get_photo_detail(self, pid):
                    return photos[pid]

                def check_photo(self, target):
                    target.__dict__.update(photos[target.id].__dict__)

                def download_by_image_detail(self, image, path, **kwargs):
                    color = "red" if image.from_photo.id == "900" else "blue"
                    Image.new("RGB", (8, 8), color).save(path, lossless=True)

            with patch.object(jmcomic.JmOption, "build_jm_client", return_value=Client()):
                output = real_download({"jm_id": "123"}, settings, lambda **fields: None, threading.Event())
            pages = PdfReader(output).pages
            self.assertEqual(len(pages), 4)
            pixels = [page.images[0].image.getpixel((0, 0)) for page in pages]
            self.assertTrue(all(pixel[0] > 240 for pixel in pixels[:2]))
            self.assertTrue(all(pixel[2] > 240 for pixel in pixels[2:]))

    def test_real_adapter_progress_paths_cache_and_partial_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings("admin", "password", "real", Path(temp), Path(temp) / "data")
            album = jmcomic.JmAlbumDetail("123", "1", "测试标题", [], 2, "", "", "0", "0", 0, [], [], ["作者甲", "作者乙"], [])
            photo = jmcomic.JmPhotoDetail("123", "测试章节", 0, 1, scramble_id="1",
                                         page_arr=["00002.webp", "00010.webp"],
                                         data_original_domain="example.invalid", from_album=album)

            class FakeClient:
                calls = 0
                fail = False
                cancel_event = None

                def get_album_detail(self, _):
                    return album

                def get_photo_detail(self, _):
                    return photo

                def check_photo(self, target):
                    target.__dict__.update(photo.__dict__)

                def download_by_image_detail(self, image, path, **kwargs):
                    self.calls += 1
                    if self.fail and image.index == 2:
                        raise OSError("simulated network failure")
                    Image.new("RGB", (8, 8), color="red" if image.index == 1 else "blue").save(path, lossless=True)
                    if self.cancel_event:
                        self.cancel_event.set()

            client = FakeClient()
            progress = {}
            task = {"jm_id": "123"}
            with patch.object(jmcomic.JmOption, "build_jm_client", return_value=client):
                output = real_download(task, settings, lambda **fields: progress.update(fields), threading.Event())
                self.assertEqual(progress["total"], 2)
                self.assertEqual(progress["done"], 2)
                self.assertEqual(output, str(Path(temp) / "[测试标题]+[作者甲、作者乙]+JM123" / "JM123.pdf"))
                folder = Path(output).parent
                self.assertEqual(len(list(folder.rglob("*.webp"))), 2)
                pages = PdfReader(output).pages
                self.assertEqual(len(pages), 2)
                self.assertGreater(pages[0].images[0].image.getpixel((0, 0))[0], 240)
                self.assertGreater(pages[1].images[0].image.getpixel((0, 0))[2], 240)
                image_only = real_download(task, replace(settings, export_pdf=False), lambda **fields: None, threading.Event())
                self.assertEqual(image_only, str(folder))
                self.assertEqual(client.calls, 2)
                # A completed re-submission should count existing files without downloading again.
                real_download(task, settings, lambda **fields: None, threading.Event())
                self.assertEqual(client.calls, 2)
                cached = next(folder.rglob("*.webp"))
                cached.write_bytes(b"corrupt-cache")
                real_download(task, settings, lambda **fields: None, threading.Event())
                self.assertEqual(client.calls, 3)
                with Image.open(cached) as image:
                    image.verify()
                for path in folder.rglob("*.webp"):
                    path.unlink()
                client.fail = True
                with self.assertRaises(jmcomic.PartialDownloadFailedException):
                    real_download(task, settings, lambda **fields: None, threading.Event())
                for path in folder.rglob("*.webp"):
                    path.unlink()
                client.fail = False
                client.cancel_event = threading.Event()
                with self.assertRaises(jmcomic.DownloadCancelledException):
                    real_download(task, settings, lambda **fields: None, client.cancel_event)


if __name__ == "__main__":
    unittest.main()
