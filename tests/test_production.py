import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import create_app
from app.config import Settings, load_settings
from app.downloader import DownloadError, check_storage
from app.worker import Worker
from scripts.setup import prepare


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings("admin", "a-long-test-password", "mock", self.root / "downloads", self.root / "data", min_free_mb=0)
        self.settings.downloads.mkdir()

    def config(self, password="a-long-test-password", extra=""):
        path = self.root / "config.toml"
        path.write_text(f'[auth]\nusername="admin"\npassword="{password}"\n[download]\n{extra}\n', encoding="utf-8")
        return path

    def test_real_is_default_and_example_passwords_are_rejected(self):
        path = self.config()
        with patch.dict(os.environ, {"JM_CONFIG": str(path)}, clear=True):
            self.assertEqual(load_settings().mode, "real")
            self.config("CHANGE-ME-SET-YOUR-OWN-PASSWORD")
            with self.assertRaises(ValueError):
                load_settings()
            self.config("short")
            with self.assertRaises(ValueError):
                load_settings()

    def test_compose_environment_overrides_old_mock_mode(self):
        path = self.config(extra='mode="mock"')
        with patch.dict(os.environ, {"JM_CONFIG": str(path), "JM_MODE": "real", "JM_REQUIRE_NFS": "true",
                                      "JM_SECURE_COOKIE": "true"}, clear=True):
            settings = load_settings()
            self.assertEqual(settings.mode, "real")
            self.assertTrue(settings.require_nfs)
            self.assertTrue(settings.secure_cookie)
            os.environ["JM_REQUIRE_NFS"] = "typo"
            with self.assertRaises(ValueError):
                load_settings()

    def test_setup_preserves_custom_credentials(self):
        path = self.config()
        before = path.read_text()
        prepare(self.root)
        self.assertEqual(path.read_text(), before)
        self.assertTrue((self.root / ".env").is_file())
        self.assertNotIn("password", (self.root / ".env").read_text())

    def test_setup_generates_secure_configuration(self):
        sample = Path(__file__).resolve().parents[1] / "config.example.toml"
        shutil.copyfile(sample, self.root / sample.name)
        path = self.root / "config.toml"
        prepare(self.root)
        with patch.dict(os.environ, {"JM_CONFIG": str(path)}, clear=True):
            settings = load_settings()
        self.assertEqual(settings.mode, "real")
        self.assertGreaterEqual(len(settings.password), 24)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_free_space_check_stops_download_before_any_write(self):
        with patch("app.downloader.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaises(DownloadError):
                check_storage(replace(self.settings, min_free_mb=256))
        self.assertEqual(list(self.settings.downloads.iterdir()), [])

    def test_shutdown_interrupts_active_task_and_keeps_pending_tasks(self):
        app = create_app(self.settings)
        store = app.extensions["store"]
        store.enqueue("101", "mock")
        store.enqueue("102", "mock")
        worker = Worker(store, self.settings)
        worker.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not worker.current_task:
            time.sleep(0.01)
        self.assertIsNotNone(worker.current_task)
        worker.stop()
        self.assertFalse(worker.thread.is_alive())
        tasks = {row["jm_id"]: row for row in store.list_tasks()}
        self.assertEqual(tasks["101"]["status"], "failed")
        self.assertIn("中断", tasks["101"]["message"])
        self.assertEqual(tasks["102"]["status"], "queued")

    def test_health_detects_worker_and_database_failure(self):
        app = create_app(self.settings)
        client = app.test_client()
        self.assertEqual(client.get("/health").status_code, 200)
        worker = Worker(app.extensions["store"], self.settings)
        app.extensions["worker"] = worker
        self.assertEqual(client.get("/health").status_code, 503)
        app.extensions.pop("worker")
        import sqlite3
        with patch.object(app.extensions["store"], "check", side_effect=sqlite3.OperationalError("test")):
            self.assertEqual(client.get("/health").status_code, 503)


if __name__ == "__main__":
    unittest.main()
