import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.config import Settings
from app.downloader import DownloadError, check_storage
from app.worker import Worker


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings("admin", "test-password", "mock", root / "downloads", root / "data", min_free_mb=0)
        self.settings.downloads.mkdir()
        self.app = create_app(self.settings)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions["store"]

    def login(self):
        page = self.client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        response = self.client.post("/login", data={"username": "admin", "password": "test-password", "csrf": token})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.csrf = session["csrf"]

    def post(self, path, data=None):
        return self.client.post(path, json=data or {}, headers={"X-CSRF-Token": self.csrf})

    def test_login_logout_and_protected_history(self):
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.login()
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/tasks").status_code, 200)
        self.post("/logout")
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)

    def test_csrf_required_for_login_submission_and_logout(self):
        self.assertEqual(self.client.post("/login", data={"username": "admin", "password": "test-password"}).status_code, 403)
        self.login()
        for path in ("/api/tasks", "/api/tasks/1/retry", "/logout"):
            self.assertEqual(self.client.post(path, json={"jm_id": "123"}).status_code, 403)
        self.assertEqual(self.client.post("/api/tasks", json={}, headers={"X-CSRF-Token": "错误"}).status_code, 403)

    def test_login_rate_limit(self):
        self.client.get("/login")
        with self.client.session_transaction() as session:
            token = session["csrf"]
        for _ in range(5):
            response = self.client.post("/login", data={"username": "admin", "password": "bad", "csrf": token})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.post("/login", data={"csrf": token}).status_code, 429)

    def test_number_validation_normalization_and_active_deduplication(self):
        self.login()
        for value in ("", "0", "-1", "https://example.test", "../123", "12 34", "1234567890123", "１２３"):
            self.assertEqual(self.post("/api/tasks", {"jm_id": value}).status_code, 400, value)
        first = self.post("/api/tasks", {"jm_id": " jm00114514 "})
        second = self.post("/api/tasks", {"jm_id": "114514"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json["id"], second.json["id"])
        self.assertEqual(self.store.list_tasks()[0]["jm_id"], "114514")

    def test_concurrent_submission_and_claim(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.enqueue("123", "mock"), range(12)))
        self.assertEqual(sum(created for _, created in results), 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.store.claim("mock"), range(8)))
        self.assertEqual(sum(task is not None for task in claims), 1)

    def test_mock_failure_retry_and_success_persist_across_restart(self):
        task_id, _ = self.store.enqueue("999999", "mock")
        worker = Worker(self.store, self.settings)
        with patch.object(worker.stop_event, "wait", return_value=False):
            worker.run_one()
            self.assertEqual(self.store.list_tasks()[0]["status"], "failed")
            self.login()
            self.assertEqual(self.post(f"/api/tasks/{task_id}/retry").status_code, 200)
            worker.run_one()
        task = self.store.list_tasks()[0]
        self.assertEqual((task["status"], task["done"], task["total"], task["attempt"]), ("completed", 24, 24, 2))
        self.assertTrue((Path(task["output"]) / "README.txt").is_file())
        restarted = create_app(self.settings).extensions["store"]
        self.assertEqual(restarted.list_tasks()[0]["status"], "completed")

    def test_restart_recovery_does_not_drop_queue(self):
        self.store.enqueue("101", "mock")
        self.store.enqueue("102", "mock")
        self.store.claim("mock")
        restarted = create_app(self.settings).extensions["store"]
        restarted.recover()
        tasks = {task["jm_id"]: task for task in restarted.list_tasks()}
        self.assertEqual(tasks["101"]["status"], "failed")
        self.assertIn("中断", tasks["101"]["message"])
        self.assertEqual(restarted.claim("mock")["jm_id"], "102")

    def test_retry_goes_to_end_and_rejects_conflicting_active_album(self):
        task_id, _ = self.store.enqueue("101", "mock")
        self.store.update(task_id, status="failed")
        self.store.enqueue("102", "mock")
        self.store.retry(task_id, "mock")
        self.assertEqual(self.store.claim("mock")["jm_id"], "102")
        self.store.update(task_id, status="failed")
        self.store.enqueue("101", "mock")
        with self.assertRaises(ValueError):
            self.store.retry(task_id, "mock")

    def test_other_mode_tasks_are_not_executed_or_retried(self):
        task_id, _ = self.store.enqueue("123", "real")
        self.assertIsNone(self.store.claim("mock"))
        self.store.update(task_id, status="failed")
        with self.assertRaises(ValueError):
            self.store.retry(task_id, "mock")

    def test_nfs_missing_fails_without_writing_to_local_folder(self):
        settings = replace(self.settings, require_nfs=True)
        with patch("app.downloader.filesystem_type", return_value="ext4"):
            with self.assertRaises(DownloadError):
                check_storage(settings)
        self.assertEqual(list(self.settings.downloads.iterdir()), [])

    def test_missing_download_directory_marks_failed(self):
        self.settings.downloads.rmdir()
        self.store.enqueue("123", "mock")
        Worker(self.store, self.settings).run_one()
        self.assertEqual(self.store.list_tasks()[0]["status"], "failed")
        self.assertFalse(self.settings.downloads.exists())

    def test_pagination_and_no_file_serving(self):
        self.login()
        for value in range(1, 53):
            self.store.enqueue(str(value), "mock")
        first = self.client.get("/api/tasks").json
        second = self.client.get(f"/api/tasks?before={first['next_before']}").json
        self.assertEqual(len(first["tasks"]), 50)
        self.assertEqual(len(second["tasks"]), 2)
        self.assertFalse({task["id"] for task in first["tasks"]} & {task["id"] for task in second["tasks"]})
        self.assertEqual(first["summary"]["queued"], 52)
        self.assertEqual(self.client.get("/downloads/file.jpg").status_code, 404)
        self.assertEqual(self.client.get("/config.toml").status_code, 404)

    def test_password_change_invalidates_sessions(self):
        self.login()
        updated = create_app(replace(self.settings, password="new-password"))
        updated.testing = True
        cookie = self.client.get_cookie("jm_session")
        client = updated.test_client()
        client.set_cookie("jm_session", cookie.value)
        self.assertEqual(client.get("/api/tasks").status_code, 401)

    def test_upstream_errors_do_not_expose_credentials(self):
        self.store.enqueue("123", "mock")
        with patch("app.worker.mock_download", side_effect=RuntimeError("http://user:secret@proxy")):
            Worker(self.store, self.settings).run_one()
        message = self.store.list_tasks()[0]["message"]
        self.assertNotIn("secret", message)
        self.assertIn("RuntimeError", message)


if __name__ == "__main__":
    unittest.main()
