import os
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


class PDFExportError(Exception):
    pass


def write_pdf(image_paths, output: Path, stop, report=None):
    """Use manifest order, one image per page; publish only the complete PDF."""
    paths = list(image_paths)
    if not paths:
        raise PDFExportError("没有可合并的图片")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".pdf-", suffix=".part", dir=output.parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        pdf = canvas.Canvas(str(temporary), pageCompression=1)
        pdf.setTitle(output.stem)
        pdf.setAuthor("")
        pdf.setCreator("JMComic Downloader")
        for index, path in enumerate(paths, 1):
            if stop.is_set():
                raise PDFExportError("PDF 生成已中断，请手动重试")
            try:
                with Image.open(path) as source:
                    oriented = ImageOps.exif_transpose(source)
                    if "A" in oriented.getbands() or "transparency" in oriented.info:
                        rgba = oriented.convert("RGBA")
                        image = Image.new("RGB", rgba.size, "white")
                        image.paste(rgba, mask=rgba.getchannel("A"))
                        rgba.close()
                    else:
                        image = oriented.convert("RGB")
                    oriented.close()
            except (OSError, ValueError) as error:
                raise PDFExportError("无法读取图片，原图已保留，请重试") from error
            try:
                width, height = image.size
                scale = min(595.28 / width, 14400 / height)
                page_width, page_height = width * scale, height * scale
                pdf.setPageSize((page_width, page_height))
                # Retain just one decoded page at a time, rather than loading the book as bitmaps.
                with BytesIO() as encoded:
                    image.save(encoded, "JPEG", quality=95)
                    encoded.seek(0)
                    pdf.drawImage(ImageReader(encoded), 0, 0, width=page_width, height=page_height)
                pdf.showPage()
            finally:
                image.close()
            if report:
                report(index, len(paths))
        pdf.save()
        if stop.is_set():
            raise PDFExportError("PDF 生成已中断，请手动重试")
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
        os.replace(temporary, output)
        return output
    finally:
        temporary.unlink(missing_ok=True)
