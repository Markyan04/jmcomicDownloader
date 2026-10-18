import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    jm_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    title TEXT NOT NULL DEFAULT '',
                    done INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '等待下载',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    output TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_album
                ON tasks(jm_id, mode) WHERE status IN ('queued', 'downloading');
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            db.execute("""UPDATE tasks SET status='failed', message=?, updated_at=?
                          WHERE status='downloading'""",
                       ("应用重启，下载已中断，请手动重试", now()))

    def enqueue(self, jm_id, mode):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("""SELECT id FROM tasks WHERE jm_id=? AND mode=?
                                  AND status IN ('queued','downloading')""", (jm_id, mode)).fetchone()
            if existing:
                return existing["id"], False
            cursor = db.execute("""INSERT INTO tasks(jm_id,mode,created_at,updated_at)
                                   VALUES(?,?,?,?)""", (jm_id, mode, now(), now()))
            return cursor.lastrowid, True

    def interrupt(self, task_id):
        with self.connect() as db:
            db.execute("""UPDATE tasks SET status='failed',message=?,updated_at=?
                          WHERE id=? AND status='downloading'""",
                       ("应用停止，下载已中断，请手动重试", now(), task_id))

    def check(self):
        with self.connect() as db:
            db.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()

    def claim(self, mode):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE status='queued' AND mode=? ORDER BY updated_at,id LIMIT 1",
                             (mode,)).fetchone()
            if row is None:
                return None
            db.execute("""UPDATE tasks SET status='downloading', attempt=attempt+1,
                          message='正在获取信息', updated_at=? WHERE id=?""", (now(), row["id"]))
            return dict(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())

    def update(self, task_id, **values):
        allowed = {"status", "title", "done", "total", "message", "output"}
        if not values or not set(values) <= allowed:
            raise ValueError("无效任务字段")
        values["updated_at"] = now()
        with self.connect() as db:
            fields = ", ".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE tasks SET {fields} WHERE id=?", (*values.values(), task_id))

    def retry(self, task_id, mode):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise LookupError("任务不存在")
            if row["status"] != "failed" or row["mode"] != mode:
                raise ValueError("只能重试当前模式的失败任务")
            active = db.execute("""SELECT id FROM tasks WHERE jm_id=? AND mode=?
                                AND status IN ('queued','downloading')""", (row["jm_id"], mode)).fetchone()
            if active:
                raise ValueError("这个编号已有活动任务，请等待该任务结束")
            db.execute("""UPDATE tasks SET status='queued',done=0,total=0,message='等待重试',
                          output='',updated_at=? WHERE id=?""", (now(), task_id))

    def list_tasks(self, before=None, limit=50):
        with self.connect() as db:
            if before:
                rows = db.execute("SELECT * FROM tasks WHERE id<? ORDER BY id DESC LIMIT ?", (before, limit))
            else:
                rows = db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in rows]

    def summary(self):
        result = dict.fromkeys(("queued", "downloading", "completed", "failed"), 0)
        with self.connect() as db:
            for row in db.execute("SELECT status,COUNT(*) AS n FROM tasks GROUP BY status"):
                result[row["status"]] = row["n"]
        return result
