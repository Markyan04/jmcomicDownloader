import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.config import Settings


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.settings = Settings("admin", "test-password", "mock", self.downloads, self.root / "data")
        self.app = create_app(self.settings)
        self.app.testing = True
        self.client = self.app.test_client()
        self.book = self.downloads / "旧书名 + 作者 & JM123"
        self.book.mkdir()
        self.pdf = self.book / "JM123.PDF"
        self.content = b"%PDF-1.4\n" + bytes(range(256)) * 2048 + b"\n%%EOF"
        self.pdf.write_bytes(self.content)
        self.relative = self.pdf.relative_to(self.downloads).as_posix()

    def login(self):
        page = self.client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        self.client.post("/login", data={"username": "admin", "password": "test-password", "csrf": token})

    def file(self, **kwargs):
        return self.client.get("/api/library/file", query_string={"path": self.relative}, **kwargs)

    def test_all_library_routes_and_head_require_login(self):
        for path in ("/library", "/reader"):
            self.assertEqual(self.client.get(path).status_code, 302)
        for path in ("/api/library", "/api/library/file"):
            self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.head(path).status_code, 401)

    def test_worker_policy_allows_bundled_wasm_and_static_assets_revalidate(self):
        with self.client.head("/static/vendor/pdfjs/pdf.worker.min.mjs") as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("'wasm-unsafe-eval'", response.headers["Content-Security-Policy"])
            self.assertNotIn("'unsafe-eval'", response.headers["Content-Security-Policy"])
            self.assertIn("must-revalidate", response.headers["Cache-Control"])

    def test_filesystem_library_without_task_records_and_fresh_directory_refresh(self):
        self.login()
        self.assertEqual(self.app.extensions["store"].list_tasks(), [])
        root = self.client.get("/api/library").json
        self.assertEqual(root["entries"][0]["path"], self.book.name)
        listing = self.client.get("/api/library", query_string={"path": self.book.name}).json
        self.assertEqual(listing["entries"][0]["path"], self.relative)
        self.assertEqual(listing["entries"][0]["size"], len(self.content))
        (self.book / "later.pdf").write_bytes(b"pdf")
        self.assertEqual(self.client.get("/api/library", query_string={"path": self.book.name}).json["total"], 2)
        page = self.client.get("/reader", query_string={"path": self.relative})
        self.assertEqual(page.status_code, 200)
        self.assertIn("JM123.PDF", page.text)
        self.assertIn("worker-src 'self'", page.headers["Content-Security-Policy"])

    def test_range_head_suffix_unsatisfiable_and_version_checks(self):
        self.login()
        response = self.client.head("/api/library/file", query_string={"path": self.relative})
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"")
        self.assertEqual(response.content_length, len(self.content))
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        etag = response.headers["ETag"]
        with self.file(headers={"Range": "bytes=50-149", "If-Range": etag}) as chunk:
            self.assertEqual(chunk.status_code, 206)
            self.assertEqual(chunk.data, self.content[50:150])
            self.assertEqual(chunk.content_length, 100)
            self.assertEqual(chunk.headers["Content-Range"], f"bytes 50-149/{len(self.content)}")
            self.assertIn("no-store", chunk.headers["Cache-Control"])
            self.assertEqual(chunk.mimetype, "application/pdf")
            self.assertEqual(chunk.headers["X-Accel-Buffering"], "no")
        with self.file(headers={"Range": "bytes=-6"}) as chunk:
            self.assertEqual(chunk.status_code, 206)
            self.assertEqual(chunk.data, b"\n%%EOF")
        self.assertEqual(self.file(headers={"Range": f"bytes={len(self.content)}-"}).status_code, 416)
        self.pdf.write_bytes(self.content + b"updated")
        # If-Range mismatch returns full 200; the reader must cancel it and request reload.
        with self.file(headers={"Range": "bytes=0-99", "If-Range": etag}) as changed:
            self.assertEqual(changed.status_code, 200)
            self.assertNotEqual(changed.headers["ETag"], etag)

    def test_path_traversal_hidden_symlink_and_non_pdf_are_rejected(self):
        self.login()
        outside = self.root / "private.pdf"
        outside.write_bytes(b"private")
        (self.downloads / "link.pdf").symlink_to(outside)
        (self.downloads / "internal-link.pdf").symlink_to(self.pdf)
        (self.downloads / "link-dir").symlink_to(self.root, target_is_directory=True)
        (self.downloads / ".private.pdf").write_bytes(b"private")
        (self.downloads / "photo.webp").write_bytes(b"image")
        (self.downloads / "book.pdf.part").write_bytes(b"unfinished")
        for path in ("../private.pdf", "/private.pdf", "a/../../private.pdf", "a\\..\\private.pdf",
                     "link.pdf", "internal-link.pdf", "link-dir/private.pdf", ".private.pdf",
                     "photo.webp", "book.pdf.part", self.book.name, "missing.pdf", "\x00.pdf"):
            for endpoint in ("/reader", "/api/library/file"):
                with self.subTest(path=path, endpoint=endpoint):
                    self.assertEqual(self.client.get(endpoint, query_string={"path": path}).status_code, 404)
        entries = self.client.get("/api/library").json["entries"]
        self.assertEqual([e["name"] for e in entries], [self.book.name])
        self.assertEqual(self.client.get("/api/library", query_string={"path": "../"}).status_code, 404)

    def test_pagination_filters_files_and_stays_bounded(self):
        self.login()
        for number in range(103):
            (self.book / f"{number:04}.pdf").write_bytes(b"pdf")
        (self.book / "image.jpg").touch()
        first = self.client.get("/api/library", query_string={"path": self.book.name}).json
        second = self.client.get("/api/library", query_string={"path": self.book.name, "page": 2}).json
        self.assertEqual(len(first["entries"]), 100)
        self.assertEqual(len(second["entries"]), 4)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertFalse({e["path"] for e in first["entries"]} & {e["path"] for e in second["entries"]})
        self.assertEqual(self.client.get("/api/library?page=0").status_code, 400)
        self.assertEqual(self.client.get("/api/library?page=invalid").status_code, 400)

    def test_read_errors_are_generic_and_do_not_expose_host_paths(self):
        self.login()
        with patch("app.library.send_file", side_effect=PermissionError(str(self.pdf))):
            response = self.file()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(str(self.root), response.text)

    def test_existing_tasks_and_pdf_survive_new_app_and_password_changes_revoke_file_access(self):
        store = self.app.extensions["store"]
        task_id, _ = store.enqueue("123", "mock")
        store.update(task_id, status="completed", output=str(self.pdf))
        restarted = create_app(self.settings)
        self.assertEqual(restarted.extensions["store"].list_tasks()[0]["output"], str(self.pdf))
        self.assertEqual(self.pdf.read_bytes(), self.content)
        self.login()
        updated = create_app(replace(self.settings, password="changed-password"))
        client = updated.test_client()
        client.set_cookie("jm_session", self.client.get_cookie("jm_session").value)
        self.assertEqual(client.get("/api/library/file", query_string={"path": self.relative}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
