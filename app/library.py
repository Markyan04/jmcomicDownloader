"""Read-only filesystem library; deliberately independent of download tasks."""
from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file, url_for

library = Blueprint("library", __name__)
PAGE_SIZE = 100


def local_path(value):
    root = current_app.extensions["settings"].downloads.resolve()
    # Do not normalize away traversal or follow symlinks, including links within the root.
    parts = value.split("/") if value else []
    if any(not part or part.startswith(".") or "\\" in part
           or any(ord(char) < 32 for char in part) for part in parts):
        abort(404)
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            abort(404)
    if not path.resolve().is_relative_to(root):
        abort(404)
    return path


def pdf_path(value):
    path = local_path(value)
    if path.suffix.lower() != ".pdf" or not path.is_file():
        abort(404)
    return path


@library.errorhandler(OSError)
def unavailable(error):
    # Never expose host paths or storage credentials in client errors.
    return jsonify(error="文件暂时不可读，请检查存储挂载和访问权限后刷新"), 503


@library.get("/library")
def index():
    return render_template("library.html")


@library.get("/api/library")
def listing():
    relative = request.args.get("path", "")
    directory = local_path(relative)
    if not directory.is_dir():
        abort(404)
    page = request.args.get("page", type=int) if "page" in request.args else 1
    if page is None or page < 1:
        return jsonify(error="页码无效"), 400
    entries = []
    for child in directory.iterdir():
        if (child.name.startswith(".") or child.is_symlink() or "\\" in child.name
                or any(ord(char) < 32 for char in child.name)):
            continue
        try:
            folder = child.is_dir()
            if not folder and (child.suffix.lower() != ".pdf" or not child.is_file()):
                continue
            info = child.stat()
        except FileNotFoundError:
            continue  # Files can disappear while a directory is being listed.
        path = f"{relative}/{child.name}" if relative else child.name
        entries.append(dict(name=child.name, path=path, kind="directory" if folder else "pdf",
                            size=None if folder else info.st_size))
    entries.sort(key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold()))
    total = len(entries)
    start = (page - 1) * PAGE_SIZE
    return jsonify(path=relative, entries=entries[start:start + PAGE_SIZE], page=page,
                   has_more=start + PAGE_SIZE < total, total=total)


@library.get("/reader")
def reader():
    relative = request.args.get("path", "")
    path = pdf_path(relative)
    parent = relative.rpartition("/")[0]
    return render_template("reader.html", name=path.name,
                           file_url=url_for("library.file", path=relative),
                           back_url=url_for("library.index", path=parent))


@library.get("/api/library/file")
def file():
    path = pdf_path(request.args.get("path", ""))
    # send_file uses a seekable file and stat metadata: no whole-file read into memory.
    response = send_file(path, mimetype="application/pdf", conditional=True,
                         as_attachment=False, download_name=path.name)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["X-Accel-Buffering"] = "no"
    return response
