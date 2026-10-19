import re
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.config import Settings


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.app = create_app(Settings("admin", "test-password", "mock", root / "downloads", root / "data"))
        self.app.testing = True
        self.client = self.app.test_client()

    def login(self):
        token = re.search(r'name="csrf" value="([^"]+)"', self.client.get("/login").text)[1]
        self.client.post("/login", data={"username": "admin", "password": "test-password", "csrf": token})

    def test_absolute_two_hour_expiry_is_not_extended_by_activity(self):
        start = time.time()
        with patch("app.time.time", return_value=start):
            self.login()
            with self.client.session_transaction() as session:
                self.assertEqual(session["expires_at"], start + 7200)
        self.assertEqual(self.app.permanent_session_lifetime, timedelta(hours=2))
        self.assertFalse(self.app.config["SESSION_REFRESH_EACH_REQUEST"])
        for seconds in (60, 3600, 7199):
            with patch("app.time.time", return_value=start + seconds):
                response = self.client.get("/api/tasks")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Set-Cookie", response.headers)
        with patch("app.time.time", return_value=start + 7200):
            for endpoint in ("/api/tasks", "/api/library", "/api/library/file?path=book.pdf"):
                self.assertEqual(self.client.get(endpoint).status_code, 401)
            self.assertEqual(self.client.post("/api/tasks/1/delete").status_code, 401)
            self.assertEqual(self.client.get("/").status_code, 302)
            self.assertEqual(self.client.get("/reader?path=book.pdf").status_code, 302)
            self.login()
            self.assertEqual(self.client.get("/api/tasks").status_code, 200)

    def test_old_sessions_without_absolute_expiry_require_fresh_login(self):
        self.login()
        with self.client.session_transaction() as session:
            del session["expires_at"]
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)


if __name__ == "__main__":
    unittest.main()
