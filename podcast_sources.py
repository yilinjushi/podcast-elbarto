"""Independent, versioned source snapshots for the single-writer podcast queue.

No archive, website, mail or browser side effects. Queue status is never reset on
repeat scans, including retired episodes. Run bookmark scans with one writer.
"""
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from contextlib import contextmanager

BASE_DIR = Path(__file__).resolve().parent
PROCESSING_VERSION = "1"


@contextmanager
def queue_lock(state_path: Path):
    """Cross-platform process lock; the OS releases it after a process crash.

    Hold only for queue read/modify/write, never during browser or audio work.
    """
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(state_path) + ".lock", "a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enqueue_source(*, url: str, title: str, author: str, items: list[dict]) -> dict:
    state_path = Path(os.environ.get("PODCAST_STATE_FILE") or BASE_DIR / "state" / "podcast_queue.json")
    with queue_lock(state_path):
        return _enqueue_source(url=url, title=title, author=author, items=items, state_path=state_path)


def _enqueue_source(*, url: str, title: str, author: str, items: list[dict], state_path: Path) -> dict:
    """Persist every extracted item, then atomically enqueue its content version.

    text contains only extracted body blocks; descriptive metadata is separate.
    Non-text items are preserved and included in the version hash so changing an
    image or other substantive block does not silently reuse the old snapshot.
    """
    parsed = urlsplit(url)
    match = re.fullmatch(r"/[^/]+/status/(\d+)/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} or not match:
        raise ValueError("Podcast source requires a valid HTTPS X status URL")
    article_id = "x-" + match.group(1)
    text = "\n\n".join(item["text"] for item in items if item.get("type") != "image" and item.get("text"))
    if not text.strip():
        raise ValueError("Podcast source has no readable body text")
    serialized_items = json.dumps({"title": title, "items": items}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body_hash = hashlib.sha256(serialized_items.encode("utf-8")).hexdigest()
    task_id = f"{article_id}-{body_hash}-v{PROCESSING_VERSION}"
    source_root = Path(os.environ.get("PODCAST_SOURCE_DIR") or BASE_DIR / "state" / "podcast_sources")
    queue = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"schema_version": 1, "tasks": {}}
    if queue.get("schema_version") != 1 or not isinstance(queue.get("tasks"), dict):
        raise ValueError("Unsupported podcast queue schema")
    if task_id in queue["tasks"]:
        if queue["tasks"][task_id].get("status") not in {
            "pending", "source_ready", "adapting_and_capturing", "captured", "checking",
            "ready_to_publish", "publishing", "published", "retired", "needs_login",
            "needs_review", "retry_wait", "quota_wait", "failed",
        }:
            raise ValueError("Unknown podcast task status")
        return {**queue["tasks"][task_id], "enqueued": False}
    timestamp = datetime.now(timezone.utc).isoformat()
    source_dir = source_root / article_id / body_hash
    source_path = source_dir / "source.json"
    snapshot = {
        "schema_version": 1, "article_id": article_id, "body_hash": body_hash,
        "metadata": {"title": title, "url": url, "author": author, "captured_at": timestamp, "source": "x_bookmark"},
        "text": text, "items": items,
    }
    atomic_write(source_path, json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    atomic_write(source_dir / "source.md", text + "\n")
    task = {
        "id": task_id, "article_id": article_id, "body_hash": body_hash,
        "processing_version": PROCESSING_VERSION, "status": "source_ready",
        "source_path": Path(os.path.relpath(source_path.resolve(), state_path.parent.resolve())).as_posix(), "created_at": timestamp,
        "title": title, "url": url, "author": author,
    }
    queue["tasks"][task_id] = task
    atomic_write(state_path, json.dumps(queue, ensure_ascii=False, indent=2) + "\n")
    return {**task, "enqueued": True}
