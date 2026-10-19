import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from datetime import timedelta

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from .config import Settings, load_settings
from .library import library
from .store import Store


def create_app(settings: Settings | None = None):
    settings = settings or load_settings()
    settings.data.mkdir(parents=True, exist_ok=True)
    secret_file = settings.data / "session.key"
    try:
        with open(secret_file, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
            file.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_file.read_text().strip(), SESSION_COOKIE_NAME="jm_session",
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.secure_cookie, PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=4096,
    )
    if len(app.secret_key) < 32:
        raise RuntimeError("会话密钥无效，请检查 data/session.key")
    store = Store(settings.data / "tasks.sqlite3")
    app.extensions["store"] = store
    app.extensions["settings"] = settings
    app.register_blueprint(library)
    # Password changes invalidate existing sessions without needing a user database.
    identity = hashlib.sha256(f"{settings.username}\0{settings.password}".encode()).hexdigest()
    attempts = OrderedDict()
    attempts_lock = threading.Lock()

    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf
    app.jinja_env.globals["session_seconds_remaining"] = lambda: (
        max(0, session["expires_at"] - time.time()) if "expires_at" in session else ""
    )

    @app.before_request
    def protect():
        if request.path == "/health":
            return None
        if session.get("identity"):
            expires = session.get("expires_at")
            if not isinstance(expires, (int, float)) or time.time() >= expires:
                session.clear()
        if request.path.startswith("/api/") and session.get("identity") != identity:
            return jsonify(error="请先登录"), 401
        if request.blueprint == "library" and session.get("identity") != identity:
            return redirect(url_for("login"))
        if request.method == "POST":
            expected = session.get("csrf", "")
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
            if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
                if request.path.startswith("/api/"):
                    return jsonify(error="页面会话已失效，请刷新后重试"), 403
                return render_template("login.html", error="页面已失效，请重新登录"), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = (
            "public, max-age=0, must-revalidate" if request.endpoint == "static" else "no-store"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if (request.endpoint == "library.reader" or
                (request.endpoint == "static" and request.view_args.get("filename") == "vendor/pdfjs/pdf.worker.min.mjs")):
            # PDF.js runs a same-origin Worker and can use bundled WASM image decoders.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; "
                "worker-src 'self'; font-src 'self' blob:; img-src 'self' data: blob:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        return response

    @app.get("/health")
    def health():
        worker = app.extensions.get("worker")
        if worker and (not worker.thread.is_alive() or worker.stop_event.is_set()):
            return jsonify(status="unavailable"), 503
        try:
            store.check()
        except sqlite3.Error:
            return jsonify(status="unavailable"), 503
        return jsonify(status="ok")

    @app.errorhandler(sqlite3.Error)
    def storage_error(error):
        app.logger.error("Task database unavailable: %s", type(error).__name__)
        if request.path.startswith("/api/"):
            return jsonify(error="任务数据库暂时不可用，请稍后重试"), 503
        return "任务数据库暂时不可用，请检查本地数据目录", 503

    @app.errorhandler(413)
    def request_too_large(error):
        return jsonify(error="请求内容过长"), 413

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("identity") == identity:
            return redirect(url_for("index"))
        error, status = "", 200
        if request.method == "POST":
            address = request.remote_addr or "unknown" # No untrusted X-Forwarded-For.
            current = time.monotonic()
            with attempts_lock:
                recent = attempts.setdefault(address, deque())
                attempts.move_to_end(address)
                while recent and recent[0] < current - 60:
                    recent.popleft()
                limited = len(recent) >= 5
                if not limited:
                    recent.append(current)
                if len(attempts) > 1024:
                    attempts.popitem(last=False)
            if limited:
                error, status = "尝试次数过多，请一分钟后再试", 429
            elif (hmac.compare_digest(request.form.get("username", "").encode(), settings.username.encode())
                  and hmac.compare_digest(request.form.get("password", "").encode(), settings.password.encode())):
                with attempts_lock:
                    attempts.pop(address, None)
                session.clear()
                session.permanent = True
                session["identity"] = identity
                session["expires_at"] = time.time() + 2 * 60 * 60
                csrf()
                return redirect(url_for("index"))
            else:
                error, status = "账号或密码不正确", 401
        return render_template("login.html", error=error), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        if session.get("identity") != identity:
            return redirect(url_for("login"))
        return render_template("index.html", username=settings.username, mode=settings.mode)

    @app.get("/api/tasks")
    def tasks():
        before = request.args.get("before", type=int)
        rows = store.list_tasks(before=before, limit=51)
        has_more = len(rows) > 50
        rows = rows[:50]
        return jsonify(tasks=rows, summary=store.summary(),
                       next_before=rows[-1]["id"] if has_more else None)

    @app.post("/api/tasks")
    def enqueue():
        import re
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="请输入有效的 JM 号"), 400
        value = str(payload.get("jm_id", "")).strip()
        match = re.fullmatch(r"(?:JM)?([0-9]{1,12})", value, flags=re.IGNORECASE)
        if not match or int(match[1]) == 0:
            return jsonify(error="请输入正整数 JM 号，例如 114514 或 JM114514"), 400
        try:
            task_id, created = store.enqueue(str(int(match[1])), settings.mode)
        except ValueError as error:
            return jsonify(error=str(error)), 409
        worker = app.extensions.get("worker")
        if worker:
            worker.wake.set()
        return jsonify(id=task_id, created=created), 201 if created else 200

    @app.post("/api/tasks/<int:task_id>/retry")
    def retry(task_id):
        try:
            store.retry(task_id, settings.mode)
        except LookupError as error:
            return jsonify(error=str(error)), 404
        except ValueError as error:
            return jsonify(error=str(error)), 409
        worker = app.extensions.get("worker")
        if worker:
            worker.wake.set()
        return jsonify(ok=True)

    @app.post("/api/tasks/<int:task_id>/delete")
    def delete_task(task_id):
        worker = app.extensions.get("worker")
        if not worker:
            return jsonify(error="下载服务暂时不可用，请稍后重试"), 503
        try:
            removed = worker.request_delete(task_id)
        except LookupError as error:
            return jsonify(error=str(error)), 404
        except ValueError as error:
            return jsonify(error=str(error)), 409
        return jsonify(ok=True, removed=removed), 200 if removed else 202

    return app
