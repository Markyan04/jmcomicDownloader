import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import create_app
from app.config import Settings
from app.pdf_export import write_pdf
from app.worker import Worker


class TaskDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings("admin", "test-password", "mock", root / "downloads", root / "data", min_free_mb=0)
        self.settings.downloads.mkdir()
        self.app = create_app(self.settings)
        self.app.testing = True
        self.store = self.app.extensions["store"]
        self.worker = Worker(self.store, self.settings)
        self.app.extensions["worker"] = self.worker
        self.client = self.app.test_client()

    def login(self):
        token = re.search(r'name="csrf" value="([^"]+)"', self.client.get("/login").text)[1]
        self.client.post("/login", data={"username": "admin", "password": "test-password", "csrf": token})
        with self.client.session_transaction() as session:
            self.csrf = session["csrf"]

    def post_delete(self, task_id):
        return self.client.post(f"/api/tasks/{task_id}/delete", json={}, headers={"X-CSRF-Token": self.csrf})

    def completed(self, jm_id="123"):
        task_id, _ = self.store.enqueue(jm_id, "mock")
        folder = self.settings.downloads / f"[Book]+[Author]+JM{jm_id}"
        (folder / "chapter").mkdir(parents=True, exist_ok=True)
        (folder / "chapter" / "image.webp").write_bytes(b"test")
        pdf = folder / f"JM{jm_id}.pdf"
        pdf.write_bytes(b"test-pdf")
        (folder / ".pdf-leftover.part").touch()
        self.store.update(task_id, status="completed", output=str(pdf))
        return task_id, folder

    def test_delete_endpoint_requires_login_and_csrf(self):
        self.assertEqual(self.client.post("/api/tasks/1/delete").status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/api/tasks/1/delete", json={}).status_code, 403)
        self.assertEqual(self.post_delete(100).status_code, 404)

    def test_queued_cancel_and_completed_recursive_delete(self):
        self.login()
        queued, _ = self.store.enqueue("456", "mock")
        self.assertEqual(self.post_delete(queued).status_code, 200)
        self.assertIsNone(self.store.get(queued))
        task_id, folder = self.completed()
        outside = self.settings.data / "keep.txt"
        outside.write_text("unrelated")
        (folder / "external-link").symlink_to(outside)
        response = self.post_delete(task_id)
        self.assertTrue(response.json["removed"])
        self.assertIsNone(self.store.get(task_id))
        self.assertFalse(folder.exists())
        self.assertEqual(outside.read_text(), "unrelated")

    def test_active_cancel_waits_for_writer_and_does_not_stop_other_tasks(self):
        task_id, _ = self.store.enqueue("123", "mock")
        other_id, other_folder = self.completed("456")
        folder = self.settings.downloads / "[Book]+[Author]+JM123"
        started, stopped, release = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def download(task, settings, report, stop):
            report(output=str(folder))
            folder.mkdir()
            (folder / "first.webp").touch()
            started.set()
            self.assertTrue(stop.cancel.wait(3))
            stopped.set()
            self.assertTrue(release.wait(3))
            # Model an image request that was already writing when cancellation arrived.
            (folder / "in-flight.webp").touch()
            return str(folder)

        with patch("app.worker.mock_download", side_effect=download):
            thread = threading.Thread(target=self.worker.run_one)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                self.assertTrue(self.worker.request_delete(other_id))
                self.assertFalse(other_folder.exists())
                self.assertFalse(self.worker.request_delete(task_id))
                self.assertTrue(stopped.wait(3))
                self.assertTrue(folder.exists())
                self.assertEqual(self.store.get(task_id)["delete_requested"], 1)
                with self.assertRaises(ValueError):
                    self.store.enqueue("123", "mock")
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertIsNone(self.store.get(task_id))
        self.assertFalse(folder.exists())
        self.assertFalse(self.worker.stop_event.is_set())
        self.assertTrue(self.store.enqueue("123", "mock")[1])

    def test_cancel_during_pdf_export_removes_temporary_and_original_files(self):
        task_id, _ = self.store.enqueue("123", "mock")
        folder = self.settings.downloads / "[Book]+[Author]+JM123"

        def download(task, settings, report, stop):
            report(output=str(folder))
            folder.mkdir()
            image = folder / "image.webp"
            Image.new("RGB", (40, 60), "red").save(image)

            def progress(*_):
                self.assertFalse(self.worker.request_delete(task_id))
                self.assertTrue(folder.exists())

            return str(write_pdf([image, image], folder / "JM123.pdf", stop, progress))

        with patch("app.worker.mock_download", side_effect=download):
            self.worker.run_one()
        self.assertFalse(folder.exists())
        self.assertIsNone(self.store.get(task_id))

    def test_cleanup_failures_preserve_record_and_allow_delete_again(self):
        task_id, folder = self.completed()
        with patch("app.worker.remove_task_files", side_effect=PermissionError("private path")):
            with self.assertRaises(ValueError):
                self.worker.request_delete(task_id)
        row = self.store.get(task_id)
        self.assertEqual((row["status"], row["delete_requested"]), ("failed", 0))
        self.assertNotIn("private path", row["message"])
        self.store.retry(task_id, "mock")
        self.assertEqual(self.store.get(task_id)["output"], row["output"])
        self.assertTrue(self.worker.request_delete(task_id))
        self.assertFalse(folder.exists())
        self.assertIsNone(self.store.get(task_id))

    def test_same_album_active_task_blocks_deleting_shared_files(self):
        task_id, folder = self.completed()
        other, _ = self.store.enqueue("123", "mock")
        with self.assertRaises(ValueError):
            self.worker.request_delete(task_id)
        self.assertTrue(folder.exists())
        self.assertIsNotNone(self.store.get(other))

    def test_restart_resumes_pending_cleanup_before_claiming_next_task(self):
        task_id, folder = self.completed()
        self.store.update(task_id, status="downloading")
        self.store.request_delete(task_id)
        next_id, _ = self.store.enqueue("456", "mock")
        restarted = create_app(self.settings).extensions["store"]
        restarted.recover()
        Worker(restarted, self.settings).run_one()
        self.assertFalse(folder.exists())
        self.assertIsNone(restarted.get(task_id))
        self.assertEqual(restarted.get(next_id)["status"], "queued")

    def test_root_external_paths_and_directory_symlinks_are_never_removed(self):
        external = self.settings.data / "external"
        external.mkdir()
        keep = external / "keep.txt"
        keep.write_text("keep")
        link = self.settings.downloads / "linked"
        link.symlink_to(external, target_is_directory=True)
        mock_root = self.settings.downloads / "_mock"
        mock_root.mkdir()
        outputs = [self.settings.downloads, self.settings.downloads / "book.pdf", external, link, mock_root]
        for index, output in enumerate(outputs, 1):
            with self.subTest(output=output.name):
                task_id, _ = self.store.enqueue(str(index), "mock")
                self.store.update(task_id, status="completed", output=str(output))
                with self.assertRaises(ValueError):
                    self.worker.request_delete(task_id)
                self.assertIsNotNone(self.store.get(task_id))
                self.assertTrue(keep.exists())
                self.assertTrue(self.settings.downloads.is_dir())

    def test_failed_legacy_task_without_path_uses_exact_album_directory(self):
        task_id, _ = self.store.enqueue("123", "real")
        folder = self.settings.downloads / "JM123"
        neighbor = self.settings.downloads / "JM1234"
        folder.mkdir()
        neighbor.mkdir()
        self.store.update(task_id, status="failed", title="Legacy title")
        self.worker.request_delete(task_id)
        self.assertFalse(folder.exists())
        self.assertTrue(neighbor.exists())

    def test_old_mount_path_is_rebased_without_touching_external_resources(self):
        task_id, folder = self.completed()
        previous_mount = self.settings.data / folder.name
        previous_mount.mkdir()
        keep = previous_mount / "JM123.pdf"
        keep.write_text("external copy")
        self.store.update(task_id, output=str(keep))
        self.worker.request_delete(task_id)
        self.assertFalse(folder.exists())
        self.assertEqual(keep.read_text(), "external copy")

    def test_new_submission_waits_until_recursive_cleanup_finishes(self):
        task_id, folder = self.completed()
        started, release = threading.Event(), threading.Event()
        errors = []
        from app.task_files import remove_task_files

        def cleanup(task, settings):
            started.set()
            self.assertTrue(release.wait(3))
            remove_task_files(task, settings)

        def request():
            try:
                self.worker.request_delete(task_id)
            except Exception as error:
                errors.append(error)

        with patch("app.worker.remove_task_files", side_effect=cleanup):
            thread = threading.Thread(target=request)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                with self.assertRaises(ValueError):
                    self.store.enqueue("123", "mock")
                self.assertTrue(folder.exists())
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.assertFalse(folder.exists())
        self.assertTrue(self.store.enqueue("123", "mock")[1])

    def test_existing_database_gets_delete_flag_without_losing_rows(self):
        task_id, folder = self.completed()
        with sqlite3.connect(self.store.path) as db:
            db.execute("ALTER TABLE tasks DROP COLUMN delete_requested")
        restarted = create_app(self.settings).extensions["store"]
        self.assertEqual(restarted.get(task_id)["delete_requested"], 0)
        self.assertTrue(folder.exists())


if __name__ == "__main__":
    unittest.main()
