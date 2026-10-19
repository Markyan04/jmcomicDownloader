"""Delete task-owned directories inside the configured download root only."""
import re
import shutil
from pathlib import Path


def remove_task_files(task, settings):
    root = settings.downloads.resolve()
    if not root.is_dir():
        raise OSError("Download root unavailable")
    if settings.require_nfs:
        from .downloader import filesystem_type
        if filesystem_type(root) not in ("nfs", "nfs4"):
            raise OSError("Download mount unavailable")

    candidates = []
    jm_id = re.escape(task["jm_id"])

    def matches_album(name):
        return re.fullmatch(rf"(?:JM)?{jm_id}|.+\+JM{jm_id}", name)

    if task["output"]:
        output = Path(task["output"])
        if not output.is_absolute():
            raise ValueError("Task output must be absolute")
        folder = output.parent if output.suffix.lower() == ".pdf" else output
        if not folder.is_relative_to(settings.downloads.absolute()) and not folder.is_relative_to(root):
            # Rebase recognized names when an old host/container mount has changed;
            # the old external path is never touched.
            if matches_album(folder.name):
                folder = root / folder.name
            elif task["mode"] == "mock" and folder.name == f"task-{task['id']}" and folder.parent.name == "_mock":
                folder = root / "_mock" / folder.name
            else:
                raise ValueError("Unrecognized external task directory")
        candidates.append(folder)
    elif task["mode"] == "mock":
        candidates.append(root / "_mock" / f"task-{task['id']}")
    elif task["title"]:
        # Older failed tasks did not record a path until completion. Only accept
        # one unambiguous legacy directory, never a recursive substring search.
        matches = [path for path in root.iterdir()
                   if matches_album(path.name)]
        if len(matches) > 1:
            raise ValueError("Ambiguous legacy task directory")
        candidates.extend(matches)

    for folder in candidates:
        try:
            relative = folder.relative_to(settings.downloads.absolute())
        except ValueError:
            relative = folder.relative_to(root)  # Refuse paths outside this mount.
        if not relative.parts or ".." in relative.parts or relative == Path("_mock"):
            raise ValueError("Refusing shared/root directory")
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Refusing symlink task directory")
        folder = root / relative
        if not folder.resolve().is_relative_to(root):
            raise ValueError("Refusing path outside root")
        if folder == settings.data.resolve() or folder in settings.data.resolve().parents:
            raise ValueError("Refusing application data directory")
        if folder.exists():
            # rmtree unlinks symlinks inside the directory rather than following them.
            shutil.rmtree(folder)
