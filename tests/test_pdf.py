import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from app.pdf_export import PDFExportError, write_pdf


class PDFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "book.pdf"

    def image(self, name, color, mode="RGB", size=(40, 60)):
        path = self.root / name
        Image.new(mode, size, color).save(path, lossless=True)
        return path

    def test_webp_order_aspect_ratio_and_transparency(self):
        first = self.image("2.webp", "red")
        second = self.image("10.webp", (0, 0, 255, 255), "RGBA", size=(60, 40))
        third = self.image("transparent.webp", (0, 0, 0, 0), "RGBA")
        progress = []
        write_pdf([first, second, third], self.output, threading.Event(), lambda i, n: progress.append((i, n)))
        pages = PdfReader(self.output).pages
        self.assertEqual(len(pages), 3)
        self.assertAlmostEqual(float(pages[0].mediabox.height) / float(pages[0].mediabox.width), 1.5, places=5)
        self.assertAlmostEqual(float(pages[1].mediabox.height) / float(pages[1].mediabox.width), 2 / 3, places=5)
        self.assertGreater(pages[0].images[0].image.getpixel((0, 0))[0], 240)
        self.assertGreater(pages[1].images[0].image.getpixel((0, 0))[2], 240)
        self.assertTrue(all(value > 240 for value in pages[2].images[0].image.getpixel((0, 0))))
        self.assertEqual(progress, [(1, 3), (2, 3), (3, 3)])
        self.assertTrue(first.exists())

    def test_corrupt_input_keeps_existing_pdf_and_cleans_temporary_file(self):
        first = self.image("first.webp", "red")
        corrupt = self.root / "broken.webp"
        corrupt.write_bytes(b"invalid")
        self.output.write_bytes(b"existing-pdf")
        with self.assertRaises(PDFExportError):
            write_pdf([first, corrupt], self.output, threading.Event())
        self.assertEqual(self.output.read_bytes(), b"existing-pdf")
        self.assertFalse(list(self.root.glob("*.part")))

    def test_cancelled_export_does_not_publish_partial_pdf(self):
        images = [self.image("first.webp", "red"), self.image("second.webp", "blue")]
        stop = threading.Event()
        with self.assertRaises(PDFExportError):
            write_pdf(images, self.output, stop, lambda *_: stop.set())
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob("*.part")))
        self.assertTrue(all(path.exists() for path in images))

    def test_empty_book_is_rejected(self):
        with self.assertRaises(PDFExportError):
            write_pdf([], self.output, threading.Event())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
